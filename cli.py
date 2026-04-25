"""Interactive CLI shell for Cadillac — project management, builds, iteration, debug."""

import glob
import json
import os
import re
import shlex
import time

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from .engine import Config, run, iterate, debug, resume
from .events import EventEmitter
from .display import create_display, LiveDisplay


HELP_TEXT = """\
Commands:
  new <task>                    Start a new build
  resume <workspace>            Resume a crashed/interrupted build
  list                          List all workspaces
  open <workspace>              Show workspace details
  iterate <workspace> [msg]     Re-run BUILD→VALIDATE with optional instruction
  debug <workspace> [target]    Focused debug on a specific failure
  lessons                       Show learned lessons
  config [key] [value]          Show or set configuration
  help                          Show this help
  quit / exit                   Exit Cadillac
"""


def _find_workspaces(base_dir: str) -> list[dict]:
    """Discover workspace directories with their status."""
    workspaces = []
    pattern = os.path.join(base_dir, "workspace-*")
    for ws_path in sorted(glob.glob(pattern), reverse=True):
        if not os.path.isdir(ws_path):
            continue
        info = {
            "path": ws_path,
            "name": os.path.basename(ws_path),
            "task": "",
            "phase": "unknown",
            "files": 0,
            "validation": {},
        }

        # Try to load progress.md for status
        progress_path = os.path.join(ws_path, "progress.md")
        if os.path.exists(progress_path):
            with open(progress_path) as f:
                content = f.read()
            # Extract task
            task_match = re.search(r"## Task\n(.+)", content)
            if task_match:
                info["task"] = task_match.group(1).strip()[:60]
            # Extract status line
            status_match = re.search(r"## Status: (\w+)", content)
            if status_match:
                info["phase"] = status_match.group(1)
            # Count checked files
            info["files"] = content.count("[x]")

        # Try plan.json for file count
        plan_path = os.path.join(ws_path, "plan.json")
        if os.path.exists(plan_path):
            try:
                with open(plan_path) as f:
                    plan = json.load(f)
                if not info["files"]:
                    info["files"] = len(plan.get("files", []))
            except (json.JSONDecodeError, IOError):
                pass

        workspaces.append(info)
    return workspaces


def _resolve_workspace(name: str, base_dir: str) -> str | None:
    """Resolve a workspace name or prefix to a full path."""
    # Exact match
    full = os.path.join(base_dir, name)
    if os.path.isdir(full):
        return full
    # Prefix match
    if not name.startswith("workspace-"):
        full = os.path.join(base_dir, f"workspace-{name}")
        if os.path.isdir(full):
            return full
    # Partial match
    workspaces = _find_workspaces(base_dir)
    matches = [w for w in workspaces if name in w["name"]]
    if len(matches) == 1:
        return matches[0]["path"]
    return None


def _print_workspaces(workspaces: list[dict], console):
    """Display workspace list."""
    if not workspaces:
        console.print("[dim]No workspaces found.[/dim]")
        return

    table = Table(title="Workspaces", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="cyan")
    table.add_column("Phase", width=10)
    table.add_column("Files", justify="right", width=5)
    table.add_column("Task", max_width=50)

    for i, ws in enumerate(workspaces, 1):
        phase_style = "green" if ws["phase"] == "COMPLETE" else "yellow"
        table.add_row(
            str(i),
            ws["name"],
            f"[{phase_style}]{ws['phase']}[/{phase_style}]",
            str(ws["files"]),
            ws["task"],
        )
    console.print(table)


def _show_workspace(ws_path: str, console):
    """Show detailed workspace info."""
    name = os.path.basename(ws_path)
    console.print(f"\n[bold]{name}[/bold]")
    console.print(f"Path: {ws_path}")

    # Show architecture
    arch_path = os.path.join(ws_path, "architecture.md")
    if os.path.exists(arch_path):
        with open(arch_path) as f:
            arch = f.read()
        console.print(Panel(arch[:1000], title="Architecture", border_style="blue"))

    # Show files
    py_files = []
    for f in sorted(os.listdir(ws_path)):
        if f.endswith(".py"):
            full = os.path.join(ws_path, f)
            with open(full) as fh:
                lines = sum(1 for _ in fh)
            py_files.append((f, lines))

    if py_files:
        file_table = Table(show_header=True, title="Files")
        file_table.add_column("File")
        file_table.add_column("Lines", justify="right")
        for f, lines in py_files:
            file_table.add_row(f, str(lines))
        console.print(file_table)

    # Show progress
    progress_path = os.path.join(ws_path, "progress.md")
    if os.path.exists(progress_path):
        with open(progress_path) as f:
            console.print(Panel(f.read()[:2000], title="Progress", border_style="dim"))


def _show_lessons(console):
    """Display learned lessons."""
    from .memory import load_lessons
    lessons = load_lessons()
    if not lessons:
        console.print("[dim]No lessons learned yet.[/dim]")
        return

    table = Table(title=f"Lessons ({len(lessons)} total)")
    table.add_column("Type", width=15)
    table.add_column("Trigger", max_width=40)
    table.add_column("Fix", max_width=40)
    table.add_column("Conf", justify="right", width=5)
    table.add_column("Used", justify="right", width=5)

    for lesson in sorted(lessons, key=lambda l: -l.confidence):
        table.add_row(
            lesson.type,
            lesson.trigger[:40],
            lesson.fix[:40],
            f"{lesson.confidence:.1f}",
            str(lesson.used),
        )
    console.print(table)


def _run_build(task: str, cfg: Config, base_dir: str, use_rich: bool = True):
    """Run a new build with display."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    workspace = os.path.join(base_dir, f"workspace-{ts}")

    display = create_display(use_rich=use_rich)
    emitter = EventEmitter()
    emitter.on(display.handle_event)

    display.start()
    try:
        run(task, workspace, cfg, emitter=emitter)
    finally:
        display.stop()


def _run_iterate(ws_path: str, instruction: str, cfg: Config, use_rich: bool = True):
    """Run iterate with display."""
    display = create_display(use_rich=use_rich)
    emitter = EventEmitter()
    emitter.on(display.handle_event)

    display.start()
    try:
        iterate(ws_path, cfg, instruction=instruction, emitter=emitter)
    finally:
        display.stop()


def _run_resume(ws_path: str, cfg: Config, use_rich: bool = True):
    """Resume a build with display."""
    display = create_display(use_rich=use_rich)
    emitter = EventEmitter()
    emitter.on(display.handle_event)

    display.start()
    try:
        resume(ws_path, cfg, emitter=emitter)
    finally:
        display.stop()


def _run_debug(ws_path: str, target: str, cfg: Config, use_rich: bool = True):
    """Run debug with display."""
    display = create_display(use_rich=use_rich)
    emitter = EventEmitter()
    emitter.on(display.handle_event)

    display.start()
    try:
        debug(ws_path, cfg, target=target, emitter=emitter)
    finally:
        display.stop()


def interactive_shell(cfg: Config, base_dir: str, use_rich: bool = True):
    """Run the interactive Cadillac shell."""
    if HAS_RICH:
        console = Console()
    else:
        # Minimal fallback
        class FallbackConsole:
            def print(self, *args, **kwargs):
                text = " ".join(str(a) for a in args)
                # Strip rich markup
                text = re.sub(r'\[/?[^\]]+\]', '', text)
                print(text)
        console = FallbackConsole()

    console.print(Panel(
        "[bold]Cadillac[/bold] — Autonomous Agent Builder\n"
        f"API: {cfg.api_url}\n"
        "Type [bold]help[/bold] for commands",
        border_style="blue",
    ))

    while True:
        try:
            raw = input("\ncadillac> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not raw:
            continue

        # Parse command
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("quit", "exit", "q"):
            console.print("Bye!")
            break

        elif cmd == "help":
            console.print(HELP_TEXT)

        elif cmd == "new":
            if not args:
                console.print("[red]Usage: new <task description>[/red]")
                continue
            task = " ".join(args)
            _run_build(task, cfg, base_dir, use_rich=use_rich)

        elif cmd in ("list", "ls", "projects"):
            workspaces = _find_workspaces(base_dir)
            _print_workspaces(workspaces, console)

        elif cmd == "open":
            if not args:
                console.print("[red]Usage: open <workspace>[/red]")
                continue
            ws_path = _resolve_workspace(args[0], base_dir)
            if ws_path:
                _show_workspace(ws_path, console)
            else:
                console.print(f"[red]Workspace not found: {args[0]}[/red]")

        elif cmd == "resume":
            if not args:
                console.print("[red]Usage: resume <workspace>[/red]")
                continue
            ws_path = _resolve_workspace(args[0], base_dir)
            if not ws_path:
                console.print(f"[red]Workspace not found: {args[0]}[/red]")
                continue
            _run_resume(ws_path, cfg, use_rich=use_rich)

        elif cmd == "iterate":
            if not args:
                console.print("[red]Usage: iterate <workspace> [instruction][/red]")
                continue
            ws_path = _resolve_workspace(args[0], base_dir)
            if not ws_path:
                console.print(f"[red]Workspace not found: {args[0]}[/red]")
                continue
            instruction = " ".join(args[1:]) if len(args) > 1 else ""
            _run_iterate(ws_path, instruction, cfg, use_rich=use_rich)

        elif cmd == "debug":
            if not args:
                console.print("[red]Usage: debug <workspace> [target][/red]")
                continue
            ws_path = _resolve_workspace(args[0], base_dir)
            if not ws_path:
                console.print(f"[red]Workspace not found: {args[0]}[/red]")
                continue
            target = " ".join(args[1:]) if len(args) > 1 else ""
            _run_debug(ws_path, target, cfg, use_rich=use_rich)

        elif cmd == "lessons":
            _show_lessons(console)

        elif cmd == "config":
            if not args:
                console.print(f"api_url  = {cfg.api_url}")
                console.print(f"api_key  = {'***' + cfg.api_key[-8:] if cfg.api_key else '(none)'}")
                console.print(f"model    = {cfg.model or '(auto-detect)'}")
                console.print(f"context  = {cfg.context_window}")
                console.print(f"stream   = {cfg.stream}")
            elif len(args) == 2:
                key, value = args
                if key == "api_url":
                    cfg.api_url = value
                elif key == "api_key":
                    cfg.api_key = value
                elif key == "model":
                    cfg.model = value if value != "auto" else None
                elif key == "context":
                    cfg.context_window = int(value)
                elif key == "stream":
                    cfg.stream = value.lower() in ("true", "1", "yes")
                else:
                    console.print(f"[red]Unknown config key: {key}[/red]")
                    continue
                console.print(f"[green]Set {key} = {value}[/green]")
            else:
                console.print("[red]Usage: config [key value][/red]")

        else:
            console.print(f"[red]Unknown command: {cmd}. Type 'help' for commands.[/red]")
