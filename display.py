"""Rich-based live terminal display for Cadillac builds."""

import time
from collections import deque

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from .events import Event


class LiveDisplay:
    """Rich Live display with file tree, activity log, and validation status."""

    def __init__(self):
        if not HAS_RICH:
            raise ImportError("rich is required for LiveDisplay: pip install rich")
        self.console = Console()
        self.live: Live | None = None

        # State
        self.phase = ""
        self.round = 0
        self.budget = 0
        self.total_rounds = 0
        self.n_files = 0
        self.elapsed_start = time.time()

        self.files: dict[str, str] = {}  # path -> status (pending, writing, done)
        self.planned_files: list[str] = []
        self.log_lines: deque[str] = deque(maxlen=25)
        self.validation: dict[str, str] = {
            "syntax": "-", "lint": "-", "framework": "-", "functional": "-", "run": "-", "tests": "-"
        }

    def handle_event(self, event: Event):
        """Route events to display updates."""
        d = event.data
        kind = event.kind

        if kind == "phase":
            self.phase = d.get("phase", "").upper()
            self.round = d.get("round", 0)
            self.budget = d.get("budget", 0)
            self.total_rounds = d.get("total_rounds", 0)
            self.n_files = d.get("n_files", 0)
        elif kind == "info":
            self.log_lines.append(f"[bold]{d.get('msg', '')}[/bold]")
        elif kind == "log":
            self.log_lines.append(d.get("msg", ""))
        elif kind == "tool_call":
            name = d.get("name", "?")
            summary = d.get("summary", "")[:80]
            self.log_lines.append(f"[cyan]>> {name}[/cyan]({summary})")
        elif kind == "tool_result":
            summary = d.get("summary", "")[:80]
            self.log_lines.append(f"[dim]<< {summary}[/dim]")
        elif kind == "tool_parallel":
            self.log_lines.append(f"[yellow]  parallel: {d.get('count', 0)} calls[/yellow]")
        elif kind == "llm_api":
            self.log_lines.append(f"[dim]API ~{d.get('input_est', '?')} in, {d.get('max_out', '?')} out[/dim]")
        elif kind == "llm":
            parts = []
            if d.get("elapsed"):
                parts.append(f"{d['elapsed']:.1f}s")
            if d.get("tokens"):
                parts.append(f"{d['tokens']} tok")
            self.log_lines.append(f"[green]{' | '.join(parts)}[/green]")
        elif kind == "file_written":
            path = d.get("path", "")
            self.files[path] = "done"
        elif kind == "validation":
            for r in d.get("results", []):
                name = r["name"]
                if r["passed"]:
                    self.validation[name] = "[green]PASS[/green]"
                else:
                    self.validation[name] = "[red]FAIL[/red]"
        elif kind == "lesson":
            self.log_lines.append(f"[magenta]LESSON: {d.get('trigger', '')}[/magenta]")
        elif kind == "error":
            self.log_lines.append(f"[red]ERROR: {d.get('msg', '')}[/red]")
        elif kind == "module_start":
            mod = d.get("module", "?")
            n = d.get("n_files", 0)
            self.log_lines.append(f"[bold cyan]MODULE {mod}[/bold cyan] — {n} files")
        elif kind == "module_complete":
            mod = d.get("module", "?")
            ok = d.get("success", False)
            status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
            self.log_lines.append(f"[bold]MODULE {mod}[/bold] — {status}")
        elif kind == "complete":
            self.log_lines.append(
                f"[bold green]{d.get('status', 'DONE')} | "
                f"{d.get('total_rounds', 0)}R | {d.get('n_files', 0)} files | "
                f"{d.get('elapsed', 0):.1f}s[/bold green]"
            )
        elif kind == "separator":
            pass  # Ignore separators in rich mode

        self._refresh()

    def set_planned_files(self, files: list[str]):
        """Set the planned file list from the manifest."""
        self.planned_files = files
        for f in files:
            if f not in self.files:
                self.files[f] = "pending"

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="files", ratio=1, minimum_size=25),
            Layout(name="log", ratio=3),
        )

        # Header
        elapsed = time.time() - self.elapsed_start
        elapsed_str = f"{int(elapsed)}s" if elapsed < 60 else f"{int(elapsed)//60}m{int(elapsed)%60}s"
        header_text = (
            f" CADILLAC  [bold]{self.phase}[/bold] "
            f"(R{self.round}/{self.budget}) | "
            f"{self.n_files} files | "
            f"Total R{self.total_rounds} | {elapsed_str}"
        )
        layout["header"].update(Panel(Text.from_markup(header_text), style="bold blue"))

        # Files panel
        file_table = Table(show_header=False, box=None, padding=(0, 1))
        file_table.add_column("status", width=2)
        file_table.add_column("name")

        # Show planned files in order, then any extras
        shown = set()
        for path in self.planned_files:
            status = self.files.get(path, "pending")
            icon = {"done": "[green]✓[/green]", "writing": "[yellow]▸[/yellow]"}.get(status, "[dim]·[/dim]")
            file_table.add_row(icon, path)
            shown.add(path)
        for path, status in self.files.items():
            if path not in shown:
                icon = "[green]✓[/green]" if status == "done" else "[dim]·[/dim]"
                file_table.add_row(icon, path)

        layout["files"].update(Panel(file_table, title="Files", border_style="dim"))

        # Log panel
        log_text = Text()
        for line in self.log_lines:
            log_text.append_text(Text.from_markup(line + "\n"))
        layout["log"].update(Panel(log_text, title="Activity", border_style="dim"))

        # Footer — validation
        val_parts = [f"{k}:{v}" for k, v in self.validation.items()]
        footer_text = f" Validation: {'  '.join(val_parts)}"
        layout["footer"].update(Panel(Text.from_markup(footer_text), style="dim"))

        return layout

    def _refresh(self):
        if self.live:
            self.live.update(self._build_layout())

    def start(self):
        self.elapsed_start = time.time()
        self.live = Live(self._build_layout(), console=self.console, refresh_per_second=4)
        self.live.start()

    def stop(self):
        if self.live:
            self.live.stop()
            self.live = None


class NullDisplay:
    """Fallback display that uses the default print handler from events.py."""

    def handle_event(self, event: Event):
        from .events import default_print_handler
        default_print_handler(event)

    def start(self):
        pass

    def stop(self):
        pass

    def set_planned_files(self, files: list[str]):
        pass


def create_display(use_rich: bool = True) -> LiveDisplay | NullDisplay:
    """Create the appropriate display based on availability and preference."""
    if use_rich and HAS_RICH:
        return LiveDisplay()
    return NullDisplay()
