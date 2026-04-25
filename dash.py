"""Zero-service TUI dashboard for cadillac builds.

Invoked via `python3 -m cadillac dash`. Opens a Rich-based full-screen
interactive view of:
  - all workspaces in the current directory (status, file count, elapsed)
  - currently-running builds (detected via build.jsonl mtime < LIVE_WINDOW_S)
  - selected project detail (progress.md render, validations, recent events)
  - aggregate memory lessons (`m` key) and phase-budget history (`b` key)

No server, no daemon, no new dependencies — rich (already required) + stdlib.
Exits cleanly on `q` / Ctrl-C with terminal attrs restored.
"""

from __future__ import annotations

import json
import os
import queue
import re
import select
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

LIVE_WINDOW_S = 30       # build.jsonl mtime within this → live build
TAIL_EVENTS = 50         # max events parsed from build.jsonl tail
TAIL_BYTES = 16 * 1024   # hard cap on bytes read from end of build.jsonl
LESSONS_TOP_N = 20       # top-confidence lessons in Memory view

# ── Data layer ───────────────────────────────────────────────────────────────


@dataclass
class WorkspaceMeta:
    """Per-workspace cached metadata. Keyed by (path, progress.md mtime)."""
    task: str = ""
    status: str = ""            # COMPLETE | STOPPED | (in-progress phase)
    phase_label: str = ""       # "Phase 7/7" or "BUILD"
    elapsed: str = ""
    n_files_done: int = 0
    n_files_total: int = 0
    n_deps_done: int = 0
    n_deps_total: int = 0
    validations: dict[str, str] = field(default_factory=dict)  # name -> PASS/FAIL/-
    lessons_applied: list[str] = field(default_factory=list)
    build_log_tail: list[str] = field(default_factory=list)
    raw_markdown: str = ""
    plan_entry_point: str = ""
    plan_modular: bool = False
    progress_mtime: float = 0.0
    jsonl_mtime: float = 0.0
    is_live: bool = False
    recent_events: list[dict] = field(default_factory=list)


@dataclass
class Workspace:
    path: Path
    name: str
    mtime: float                 # dir mtime (used for list sort only)
    meta: WorkspaceMeta = field(default_factory=WorkspaceMeta)


def discover_workspaces(root: Path) -> list[Workspace]:
    """Return workspace-* dirs sorted newest-first. Fast — only stats each dir."""
    workspaces: list[Workspace] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        if not entry.name.startswith("workspace-"):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        workspaces.append(Workspace(path=entry, name=entry.name, mtime=mtime))
    workspaces.sort(key=lambda w: -w.mtime)
    return workspaces


_PROGRESS_SECTION_RE = re.compile(r"^## (.+?)$", re.M)
_PROGRESS_STATUS_RE = re.compile(
    r"^## Status:\s*(\S+).*?Phase\s+(\S+).*?Elapsed:\s*(.+?)$", re.M
)
_PROGRESS_CHECK_RE = re.compile(r"^\s*-\s+\[([ xX!]|FAIL)\]\s+(.+?)$", re.M)


def _extract_section(content: str, heading: str) -> str:
    """Return body text of a `## <heading>` section (until next ## or EOF)."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s+|\Z)",
        re.M | re.S,
    )
    m = pattern.search(content)
    return m.group(1).strip() if m else ""


def parse_progress_md(content: str) -> WorkspaceMeta:
    """Parse the progress.md file cadillac writes each round.

    Heading-based regex parser. Missing sections are tolerated — a corrupt
    or truncated progress.md yields a partial WorkspaceMeta rather than
    raising. Never trusts widths; always applies sane caps.
    """
    meta = WorkspaceMeta(raw_markdown=content)

    # Task
    meta.task = _extract_section(content, "Task")[:500]

    # Status line
    status_line = re.search(r"^## Status:.*$", content, re.M)
    if status_line:
        # Split by | to get the three columns
        line = status_line.group(0).replace("## Status:", "").strip()
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 1:
            meta.status = parts[0].split()[0] if parts[0] else ""
        if len(parts) >= 2:
            meta.phase_label = parts[1]
        if len(parts) >= 3:
            meta.elapsed = parts[2].replace("Elapsed:", "").strip()

    # Plan — count [x] vs [ ]
    plan_body = _extract_section(content, "Plan")
    if plan_body:
        checks = _PROGRESS_CHECK_RE.findall(plan_body)
        meta.n_files_total = len(checks)
        meta.n_files_done = sum(1 for mark, _ in checks if mark.lower() == "x")

    # Dependencies
    deps_body = _extract_section(content, "Dependencies")
    if deps_body:
        checks = _PROGRESS_CHECK_RE.findall(deps_body)
        meta.n_deps_total = len(checks)
        meta.n_deps_done = sum(1 for mark, _ in checks if mark.lower() == "x")

    # Validation — 8-check pipeline. Convert [x] → PASS, [FAIL] → FAIL, [ ] → -
    val_body = _extract_section(content, "Validation")
    if val_body:
        for mark, name in _PROGRESS_CHECK_RE.findall(val_body):
            norm = name.strip().lower()
            if mark.lower() == "x":
                meta.validations[norm] = "PASS"
            elif "FAIL" in mark.upper():
                meta.validations[norm] = "FAIL"
            else:
                meta.validations[norm] = "-"

    # Lessons applied
    lessons_body = _extract_section(content, "Lessons Applied")
    if lessons_body:
        for line in lessons_body.splitlines():
            line = line.strip().lstrip("-").strip()
            if line:
                meta.lessons_applied.append(line[:200])
        meta.lessons_applied = meta.lessons_applied[:10]

    # Build log tail (last 10 lines from the Build Log section)
    log_body = _extract_section(content, "Build Log")
    if log_body:
        lines = [l.strip().lstrip("-").strip() for l in log_body.splitlines() if l.strip()]
        meta.build_log_tail = lines[-10:]

    return meta


def tail_build_jsonl(path: Path, n: int = TAIL_EVENTS) -> list[dict]:
    """Return the last N JSON events from build.jsonl without reading the whole file.

    Uses seek-to-end + read-up-to-TAIL_BYTES pattern. Parses each line leniently;
    malformed lines are silently skipped.
    """
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    try:
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(-TAIL_BYTES, 2)
                # discard the partial first line after the seek
                f.readline()
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    events: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def load_workspace_metadata(ws: Workspace) -> None:
    """Load-or-refresh ws.meta from disk. Cheap when already cached."""
    prog = ws.path / "progress.md"
    jsonl = ws.path / ".cadillac" / "build.jsonl"
    plan_json = ws.path / "plan.json"

    try:
        prog_mtime = prog.stat().st_mtime if prog.exists() else 0.0
    except OSError:
        prog_mtime = 0.0
    try:
        jsonl_mtime = jsonl.stat().st_mtime if jsonl.exists() else 0.0
    except OSError:
        jsonl_mtime = 0.0

    # Skip re-parse when nothing changed
    if (ws.meta.progress_mtime == prog_mtime
            and ws.meta.jsonl_mtime == jsonl_mtime
            and ws.meta.raw_markdown):
        ws.meta.is_live = (time.time() - jsonl_mtime) <= LIVE_WINDOW_S if jsonl_mtime else False
        return

    if prog.exists():
        try:
            with open(prog) as f:
                ws.meta = parse_progress_md(f.read())
        except OSError:
            ws.meta = WorkspaceMeta()
    else:
        ws.meta = WorkspaceMeta()

    # Plan info
    if plan_json.exists():
        try:
            with open(plan_json) as f:
                plan = json.load(f)
            ws.meta.plan_entry_point = plan.get("entry_point", "")
            ws.meta.plan_modular = bool(plan.get("modular", False))
        except (OSError, json.JSONDecodeError):
            pass

    # Recent events tail
    ws.meta.recent_events = tail_build_jsonl(jsonl) if jsonl.exists() else []

    ws.meta.progress_mtime = prog_mtime
    ws.meta.jsonl_mtime = jsonl_mtime
    ws.meta.is_live = (time.time() - jsonl_mtime) <= LIVE_WINDOW_S if jsonl_mtime else False


# ── Memory + budgets aggregation ─────────────────────────────────────────────


def aggregate_memory() -> dict[str, Any]:
    """Summarize cadillac's accumulated lesson memory.

    Returns:
        {"total": N, "top": [lesson...], "by_tag": {tag: count},
         "by_polarity": {"do": N, "dont": N}}
    """
    try:
        from cadillac.memory import load_lessons
    except Exception:
        return {"total": 0, "top": [], "by_tag": {}, "by_polarity": {}}

    lessons = load_lessons()
    if not lessons:
        return {"total": 0, "top": [], "by_tag": {}, "by_polarity": {}}

    by_tag: dict[str, int] = {}
    by_polarity = {"do": 0, "dont": 0}
    for l in lessons:
        for t in (l.tags or []):
            by_tag[t] = by_tag.get(t, 0) + 1
        by_polarity[l.polarity] = by_polarity.get(l.polarity, 0) + 1

    top = sorted(lessons, key=lambda l: (-l.confidence, -l.used))[:LESSONS_TOP_N]
    return {
        "total": len(lessons),
        "top": top,
        "by_tag": dict(sorted(by_tag.items(), key=lambda kv: -kv[1])),
        "by_polarity": by_polarity,
    }


def aggregate_phase_budgets() -> dict[str, Any]:
    """Summarize phase_budgets.jsonl: group by (tag_bucket, phase), compute stats.

    Returns:
        {"total_rows": N, "by_tag_phase": {(tag_key, phase): {mean, p90, max, count}}}
    """
    try:
        from cadillac.memory import _load_phase_history
    except Exception:
        return {"total_rows": 0, "by_tag_phase": {}}

    rows = _load_phase_history()
    if not rows:
        return {"total_rows": 0, "by_tag_phase": {}}

    buckets: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        tag_key = ",".join(sorted(r.get("tags") or [])) or "(untagged)"
        phase = r.get("phase", "?")
        buckets.setdefault((tag_key, phase), []).append(int(r.get("rounds", 0)))

    summary: dict[tuple[str, str], dict[str, float]] = {}
    for key, vals in buckets.items():
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        p90_idx = min(n - 1, int(n * 0.9))
        summary[key] = {
            "mean": sum(vals_sorted) / n,
            "p90": vals_sorted[p90_idx],
            "max": vals_sorted[-1],
            "count": n,
        }
    return {"total_rows": len(rows), "by_tag_phase": summary}


# ── Render layer ─────────────────────────────────────────────────────────────


def _format_status_badge(status: str, is_live: bool) -> str:
    """Rich-markup color-coded status badge."""
    if is_live:
        return "[bold yellow on black]● LIVE[/bold yellow on black]"
    s = (status or "").upper()
    if s == "COMPLETE":
        return "[bold green]✓ DONE[/bold green]"
    if s == "STOPPED":
        return "[bold red]✗ STOP[/bold red]"
    if s in ("PLAN", "DEPS", "SCAFFOLD", "REVIEW", "BUILD", "INTEGRATE", "VALIDATE", "PACKAGE"):
        return f"[cyan]{s}[/cyan]"
    return "[dim]?[/dim]"


def _format_validations(validations: dict[str, str]) -> str:
    """One-line validation summary, color-coded."""
    if not validations:
        return "[dim]not yet run[/dim]"
    order = ["naming", "imports", "syntax", "lint", "framework", "functional", "run", "tests"]
    parts = []
    for name in order:
        v = validations.get(name)
        if v is None:
            parts.append(f"[dim]{name[:3]}:-[/dim]")
        elif v == "PASS":
            parts.append(f"[green]{name[:3]}:✓[/green]")
        elif v == "FAIL":
            parts.append(f"[red]{name[:3]}:✗[/red]")
        else:
            parts.append(f"[dim]{name[:3]}:-[/dim]")
    return " ".join(parts)


# lazy rich imports — keep tests runnable without a TTY
def _rich():
    from rich import box
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    return {
        "box": box, "Console": Console, "Layout": Layout, "Live": Live,
        "Markdown": Markdown, "Panel": Panel, "Table": Table, "Text": Text,
    }


@dataclass
class DashState:
    view: str = "projects"        # projects | memory | budgets | events | help
    workspaces: list[Workspace] = field(default_factory=list)
    selected_idx: int = 0
    filter_text: str = ""
    filter_mode: bool = False     # typing filter
    root: Path = field(default_factory=lambda: Path.cwd())
    status_msg: str = ""
    memory_cache: dict | None = None
    budgets_cache: dict | None = None
    _last_view_change: float = 0.0

    @property
    def filtered(self) -> list[Workspace]:
        if not self.filter_text:
            return self.workspaces
        q = self.filter_text.lower()
        return [w for w in self.workspaces
                if q in w.name.lower() or q in (w.meta.task or "").lower()]

    @property
    def selected(self) -> Workspace | None:
        f = self.filtered
        if not f:
            return None
        idx = max(0, min(self.selected_idx, len(f) - 1))
        return f[idx]


def _project_list_panel(state: DashState, r: dict) -> Any:
    """Left-top pane: project list with selection indicator."""
    Table, Text, Panel, box = r["Table"], r["Text"], r["Panel"], r["box"]
    filtered = state.filtered
    if not filtered:
        body = Text("No workspaces found.\nRun cadillac from a directory with workspace-* dirs.",
                    style="dim")
        title = f"Projects (0)"
    else:
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column("sel", width=1)
        table.add_column("status", width=7)
        table.add_column("name", ratio=1)
        table.add_column("files", width=6, justify="right")
        sel_idx = max(0, min(state.selected_idx, len(filtered) - 1))
        for i, w in enumerate(filtered):
            cur = "[bold yellow]▶[/bold yellow]" if i == sel_idx else " "
            badge = _format_status_badge(w.meta.status, w.meta.is_live)
            files = f"{w.meta.n_files_done}/{w.meta.n_files_total}" if w.meta.n_files_total else "-"
            # trim workspace- prefix for space
            short = w.name.replace("workspace-", "")
            table.add_row(cur, badge, short, files)
        body = table
        title = f"Projects ({len(filtered)}/{len(state.workspaces)})"
    border = "yellow" if state.filter_text else "dim"
    if state.filter_text:
        title += f"  /{state.filter_text}"
    return Panel(body, title=title, border_style=border)


def _live_builds_panel(state: DashState, r: dict) -> Any:
    """Left-bottom pane: currently-running builds (only shown if any)."""
    Table, Text, Panel = r["Table"], r["Text"], r["Panel"]
    live = [w for w in state.workspaces if w.meta.is_live]
    if not live:
        return Panel(Text("(none)", style="dim"), title="Live", border_style="dim")
    table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    table.add_column("dot", width=1)
    table.add_column("name", ratio=1)
    table.add_column("phase", width=10)
    for w in live:
        phase = w.meta.phase_label or w.meta.status or "?"
        table.add_row("[bold yellow]●[/bold yellow]",
                      w.name.replace("workspace-", ""), phase)
    return Panel(table, title=f"Live ({len(live)})", border_style="yellow")


def _detail_panel(state: DashState, r: dict) -> Any:
    """Right pane: selected project detail."""
    Panel, Table, Text, Markdown = r["Panel"], r["Table"], r["Text"], r["Markdown"]
    ws = state.selected
    if ws is None:
        return Panel(Text("No project selected.", style="dim"),
                     title="Detail", border_style="dim")

    # Top: task + status
    top_table = Table(show_header=False, box=None, padding=(0, 1))
    top_table.add_column("k", width=10, style="bold")
    top_table.add_column("v", ratio=1)
    task = ws.meta.task or "(task not stored)"
    top_table.add_row("Task", Text(task[:300], style="white"))
    top_table.add_row("Path", Text(str(ws.path), style="dim"))
    top_table.add_row("Status", Text.from_markup(_format_status_badge(ws.meta.status, ws.meta.is_live)))
    if ws.meta.phase_label:
        top_table.add_row("Phase", Text(ws.meta.phase_label, style="cyan"))
    if ws.meta.elapsed:
        top_table.add_row("Elapsed", Text(ws.meta.elapsed, style="dim"))
    if ws.meta.plan_entry_point:
        tag = " modular" if ws.meta.plan_modular else ""
        top_table.add_row("Entry", Text(f"{ws.meta.plan_entry_point}{tag}", style="dim"))

    # Validations
    vals_text = Text.from_markup(_format_validations(ws.meta.validations))

    # Files + deps summary
    files_line = (f"Files: {ws.meta.n_files_done}/{ws.meta.n_files_total}"
                  if ws.meta.n_files_total else "Files: (no plan)")
    deps_line = (f"Deps: {ws.meta.n_deps_done}/{ws.meta.n_deps_total}"
                 if ws.meta.n_deps_total else "Deps: (none)")
    prog_text = Text(f"{files_line}   {deps_line}", style="white")

    # Recent events (last 8, one line each, color-coded by kind)
    events_table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    events_table.add_column("kind", width=12, style="cyan")
    events_table.add_column("msg", ratio=1)
    for ev in ws.meta.recent_events[-8:]:
        kind = ev.get("kind", "?")
        msg = (ev.get("msg") or ev.get("name") or
               ev.get("status") or ev.get("module") or "").strip()
        if not msg and kind == "llm":
            parts = []
            if ev.get("elapsed"): parts.append(f"{ev['elapsed']:.1f}s")
            if ev.get("tokens"): parts.append(f"{ev['tokens']} tok")
            msg = " | ".join(parts)
        events_table.add_row(kind, msg[:120])

    # Lessons applied (if any)
    lessons_text = None
    if ws.meta.lessons_applied:
        lessons_text = Text()
        for l in ws.meta.lessons_applied[:5]:
            lessons_text.append("• ", style="magenta")
            lessons_text.append(l[:150] + "\n")

    # Assemble
    from rich.console import Group
    blocks = [top_table, Text(""), Text("Validations:", style="bold"), vals_text,
              Text(""), prog_text, Text(""), Text("Recent events:", style="bold"),
              events_table]
    if lessons_text:
        blocks.extend([Text(""), Text("Lessons applied:", style="bold"), lessons_text])
    return Panel(Group(*blocks), title=f"Detail — {ws.name}", border_style="cyan")


def _memory_panel(state: DashState, r: dict) -> Any:
    Panel, Table, Text = r["Panel"], r["Table"], r["Text"]
    if state.memory_cache is None:
        state.memory_cache = aggregate_memory()
    mem = state.memory_cache
    from rich.console import Group

    # Top-line summary
    summary = Text(
        f"Total: {mem['total']} lessons  |  "
        f"Do: {mem['by_polarity'].get('do', 0)}  "
        f"Dont: {mem['by_polarity'].get('dont', 0)}",
        style="bold",
    )

    # Tag histogram (top 10)
    tag_table = Table(title="Tags", show_header=True, header_style="bold cyan")
    tag_table.add_column("tag", ratio=1)
    tag_table.add_column("count", justify="right", width=6)
    for tag, count in list(mem["by_tag"].items())[:10]:
        tag_table.add_row(tag, str(count))

    # Top lessons
    top_table = Table(title=f"Top {LESSONS_TOP_N} lessons (by confidence)",
                      show_header=True, header_style="bold cyan")
    top_table.add_column("conf", width=5, justify="right")
    top_table.add_column("used", width=4, justify="right")
    top_table.add_column("pol", width=4)
    top_table.add_column("trigger → fix", ratio=1)
    for l in mem["top"]:
        pol = "[red]DONT[/red]" if l.polarity == "dont" else "[green]DO[/green]"
        line = f"{l.trigger[:60]} → {l.fix[:60]}"
        top_table.add_row(f"{l.confidence:.2f}", str(l.used), pol, line)

    return Panel(Group(summary, Text(""), tag_table, Text(""), top_table),
                 title="Memory", border_style="magenta")


def _budgets_panel(state: DashState, r: dict) -> Any:
    Panel, Table, Text = r["Panel"], r["Table"], r["Text"]
    if state.budgets_cache is None:
        state.budgets_cache = aggregate_phase_budgets()
    bud = state.budgets_cache
    from rich.console import Group

    summary = Text(f"Total rows: {bud['total_rows']}  |  "
                   f"Buckets: {len(bud['by_tag_phase'])}", style="bold")

    table = Table(title="Phase rounds by tag-bucket",
                  show_header=True, header_style="bold cyan")
    table.add_column("tag bucket", ratio=2)
    table.add_column("phase", width=10)
    table.add_column("n", width=4, justify="right")
    table.add_column("mean", width=6, justify="right")
    table.add_column("p90", width=6, justify="right")
    table.add_column("max", width=6, justify="right")
    # Sort by count desc then phase name
    sorted_rows = sorted(
        bud["by_tag_phase"].items(),
        key=lambda kv: (-kv[1]["count"], kv[0][1], kv[0][0]),
    )
    for (tag_key, phase), stats in sorted_rows[:40]:
        table.add_row(tag_key[:50], phase, str(stats["count"]),
                      f"{stats['mean']:.1f}", str(stats["p90"]), str(stats["max"]))
    return Panel(Group(summary, Text(""), table),
                 title="Budgets", border_style="green")


def _events_panel(state: DashState, r: dict) -> Any:
    Panel, Table, Text = r["Panel"], r["Table"], r["Text"]
    from rich.console import Group
    ws = state.selected
    if ws is None:
        return Panel(Text("No project selected.", style="dim"),
                     title="Events", border_style="dim")
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("kind", width=14)
    table.add_column("msg", ratio=1)
    for ev in ws.meta.recent_events:
        kind = ev.get("kind", "?")
        msg = (ev.get("msg") or ev.get("name") or
               ev.get("status") or ev.get("module") or "").strip()
        if not msg and kind == "llm":
            parts = []
            if ev.get("elapsed"): parts.append(f"{ev['elapsed']:.1f}s")
            if ev.get("tokens"): parts.append(f"{ev['tokens']} tok")
            msg = " | ".join(parts)
        table.add_row(kind, msg[:200])
    live_badge = "[yellow]● LIVE[/yellow]" if ws.meta.is_live else ""
    title = f"Events — {ws.name} {live_badge}"
    return Panel(table, title=title, border_style="yellow" if ws.meta.is_live else "cyan")


def _help_panel(state: DashState, r: dict) -> Any:
    Panel, Table, Text = r["Panel"], r["Table"], r["Text"]
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="bold yellow")
    t.add_column("v")
    pairs = [
        ("↑/↓ or j/k", "move selection up/down"),
        ("Enter", "deep-view (events for selected)"),
        ("r", "refresh from disk"),
        ("/", "filter by name or task text"),
        ("Esc", "clear filter / back to projects"),
        ("m", "memory view (lessons)"),
        ("b", "budgets view (phase history)"),
        ("e", "events view (build.jsonl tail)"),
        ("?", "this help"),
        ("q or Ctrl-C", "quit"),
    ]
    for k, v in pairs:
        t.add_row(k, v)
    return Panel(t, title="Keys", border_style="blue")


def build_frame(state: DashState, r: dict) -> Any:
    """Build the full Rich Layout for the current state."""
    Layout, Panel, Text = r["Layout"], r["Panel"], r["Text"]
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )

    # Header — current mode + filter
    live_count = sum(1 for w in state.workspaces if w.meta.is_live)
    mode = state.view.upper()
    filt = f"  /{state.filter_text}" if state.filter_text else ""
    status_line = f"  {state.status_msg}" if state.status_msg else ""
    header = Text.from_markup(
        f" CADILLAC DASH   [bold]{mode}[/bold]   "
        f"{len(state.workspaces)} projects   "
        f"[yellow]{live_count} live[/yellow]{filt}{status_line}",
    )
    layout["header"].update(Panel(header, style="bold blue"))

    # Body — depends on view
    if state.view == "projects":
        layout["body"].split_row(
            Layout(name="left", ratio=1, minimum_size=28),
            Layout(name="right", ratio=2),
        )
        layout["body"]["left"].split_column(
            Layout(_project_list_panel(state, r), name="plist", ratio=2),
            Layout(_live_builds_panel(state, r), name="live", size=8),
        )
        layout["body"]["right"].update(_detail_panel(state, r))
    elif state.view == "memory":
        layout["body"].update(_memory_panel(state, r))
    elif state.view == "budgets":
        layout["body"].update(_budgets_panel(state, r))
    elif state.view == "events":
        layout["body"].update(_events_panel(state, r))
    elif state.view == "help":
        layout["body"].update(_help_panel(state, r))

    # Footer — key hints
    keys_hint = (" [bold]?[/bold] help  "
                 "[bold]↑↓[/bold] nav  "
                 "[bold]m[/bold] memory  "
                 "[bold]b[/bold] budgets  "
                 "[bold]e[/bold] events  "
                 "[bold]r[/bold] refresh  "
                 "[bold]/[/bold] filter  "
                 "[bold]q[/bold] quit ")
    layout["footer"].update(Panel(Text.from_markup(keys_hint), style="dim"))
    return layout


# ── Input thread ─────────────────────────────────────────────────────────────


class _KeyboardReader:
    """Put the TTY in cbreak mode + read keys in a daemon thread.

    On start(), saves termios attrs and sets cbreak. On stop(), restores.
    Safe to call stop() multiple times. If stdin isn't a TTY, no-ops
    (the main loop will fall back to a 'q'-to-quit timer behavior).
    """

    def __init__(self, q: queue.Queue):
        self.q = q
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._orig_attrs = None
        self._fd = sys.stdin.fileno() if hasattr(sys.stdin, "fileno") else -1

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self._fd < 0 or not sys.stdin.isatty():
            return False
        self._orig_attrs = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._orig_attrs is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._orig_attrs)
            except Exception:
                pass
            self._orig_attrs = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                return
            if not ch:
                return
            # Escape sequences for arrow keys: ESC [ A/B/C/D
            if ch == "\x1b":
                # Try to read 2 more within a short window
                if select.select([sys.stdin], [], [], 0.01)[0]:
                    ch2 = sys.stdin.read(1)
                    if ch2 == "[" and select.select([sys.stdin], [], [], 0.01)[0]:
                        ch3 = sys.stdin.read(1)
                        mapping = {"A": "up", "B": "down", "C": "right", "D": "left"}
                        if ch3 in mapping:
                            self.q.put(mapping[ch3])
                            continue
                self.q.put("esc")
            elif ch == "\x03":  # Ctrl-C
                self.q.put("q")
            elif ch in ("\r", "\n"):
                self.q.put("enter")
            elif ch == "\x7f":  # backspace
                self.q.put("backspace")
            else:
                self.q.put(ch)


# ── Main loop ────────────────────────────────────────────────────────────────


def _handle_key(key: str, state: DashState) -> bool:
    """Return False to exit the dash loop."""
    if state.filter_mode:
        # Typing mode — most keys go to filter_text
        if key == "enter" or key == "esc":
            state.filter_mode = False
            if key == "esc":
                state.filter_text = ""
            return True
        if key == "backspace":
            state.filter_text = state.filter_text[:-1]
            state.selected_idx = 0
            return True
        if len(key) == 1 and key.isprintable():
            state.filter_text += key
            state.selected_idx = 0
        return True

    if key == "q":
        return False
    if key in ("up", "k"):
        state.selected_idx = max(0, state.selected_idx - 1)
    elif key in ("down", "j"):
        state.selected_idx = min(max(0, len(state.filtered) - 1),
                                 state.selected_idx + 1)
    elif key == "m":
        state.view = "memory"
    elif key == "b":
        state.view = "budgets"
    elif key == "e":
        state.view = "events"
    elif key == "enter":
        state.view = "events"
    elif key == "?":
        state.view = "help"
    elif key == "esc":
        state.view = "projects"
        state.filter_text = ""
        state.filter_mode = False
    elif key == "r":
        state.workspaces = discover_workspaces(state.root)
        for w in state.workspaces:
            load_workspace_metadata(w)
        state.memory_cache = None
        state.budgets_cache = None
        state.status_msg = "refreshed"
    elif key == "/":
        state.filter_mode = True
        state.status_msg = "filter mode — Enter to commit, Esc to clear"
    return True


def run(root: str | None = None) -> int:
    """Entry point for the dash subcommand. Returns process exit code."""
    r = _rich()
    state = DashState(root=Path(root or os.getcwd()))
    state.workspaces = discover_workspaces(state.root)
    if not state.workspaces:
        sys.stderr.write(
            f"No workspace-* directories found in {state.root}\n"
            f"Run cadillac builds from here first, or cd into a directory with builds.\n"
        )
        return 1

    # Initial metadata load (O(n_workspaces), < 100ms for 91 workspaces)
    for w in state.workspaces:
        load_workspace_metadata(w)

    input_q: queue.Queue = queue.Queue()
    keyboard = _KeyboardReader(input_q)
    keyboard_active = keyboard.start()

    Live = r["Live"]
    Console = r["Console"]
    console = Console()

    # If we can't get cbreak (e.g. stdout isn't a TTY), fall back to non-interactive
    # snapshot render.
    if not keyboard_active:
        console.print(build_frame(state, r))
        console.print("\n[dim](non-interactive: run in a TTY for live dashboard)[/dim]")
        return 0

    try:
        with Live(build_frame(state, r), console=console,
                  refresh_per_second=4, screen=True) as live:
            last_live_check = time.time()
            while True:
                try:
                    key = input_q.get(timeout=0.25)
                    if not _handle_key(key, state):
                        break
                    # Refresh selected project's metadata after any key
                    if state.selected:
                        load_workspace_metadata(state.selected)
                    state.status_msg = ""
                except queue.Empty:
                    pass

                # Cheap periodic live-build refresh — only touch the selected
                # project if it's flagged live.
                now = time.time()
                if now - last_live_check > 2.0:
                    last_live_check = now
                    if state.selected and state.selected.meta.is_live:
                        load_workspace_metadata(state.selected)
                    # Also recheck live-status of the full list (one stat each)
                    for w in state.workspaces:
                        jsonl = w.path / ".cadillac" / "build.jsonl"
                        if jsonl.exists():
                            try:
                                w.meta.jsonl_mtime = jsonl.stat().st_mtime
                                w.meta.is_live = (now - w.meta.jsonl_mtime) <= LIVE_WINDOW_S
                            except OSError:
                                pass

                live.update(build_frame(state, r))
    except KeyboardInterrupt:
        pass
    finally:
        keyboard.stop()
    return 0
