#!/usr/bin/env python3
"""Cadillac — autonomous application builder with learning and validation."""

import argparse
import os
import sys
import time

# Allow running as script or module
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cadillac.engine import Config, run, iterate, debug, resume, build, enhance
from cadillac.events import EventEmitter
from cadillac.display import create_display


def compute_context_budget(context_window: int) -> int:
    """Derive max_context_tokens (messages+codemap trim target) from model window.

    Rationale: chat() caps output at 16384 tokens, and we need ~4K for tool-call
    noise, token-estimator slack, and the latest user-message turn appended
    post-trim — a base ~20K reserve. We also reserve an additional 5% of the
    window as dynamic headroom for context-growth surges (observed peaks hitting
    ~92K of 131K put us within 9% of ceiling). For small windows (≤32K) this
    heuristic is too aggressive, so fall back to 60% instead.

    Examples: 65K→42K (64%), 131K→105K (80%), 200K→170K (85%).
    """
    if context_window <= 32768:
        return int(context_window * 0.60)
    # Base reserve + 5%-of-window dynamic headroom for burst safety.
    return context_window - 20000 - int(context_window * 0.05)


def _detect_context_window(api_url: str, timeout: float = 3.0) -> int | None:
    """GET /v1/models and return max_model_len; None on any failure (caller falls back)."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{api_url.rstrip('/')}/models", timeout=timeout) as r:
            data = json.loads(r.read())
        max_len = int(data["data"][0]["max_model_len"])
        return max_len if max_len > 0 else None
    except Exception:
        return None


def _make_emitter(use_rich: bool = True):
    """Create an emitter wired to the appropriate display."""
    display = create_display(use_rich=use_rich and sys.stdout.isatty())
    emitter = EventEmitter()
    emitter.on(display.handle_event)
    return emitter, display


def cmd_new(args, cfg):
    """Run a new build."""
    if args.workspace:
        workspace = os.path.abspath(args.workspace)
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        workspace = os.path.abspath(f"workspace-{ts}")

    emitter, display = _make_emitter(use_rich=not args.plain)
    display.start()
    try:
        run(args.task, workspace, cfg, emitter=emitter)
    finally:
        display.stop()


def cmd_list(args, cfg):
    """List workspaces."""
    from cadillac.cli import _find_workspaces, _print_workspaces
    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        class Console:
            def print(self, *a, **kw): print(*a)
        console = Console()
    workspaces = _find_workspaces(os.getcwd())
    _print_workspaces(workspaces, console)


def cmd_iterate(args, cfg):
    """Re-run BUILD→VALIDATE on existing workspace."""
    from cadillac.cli import _resolve_workspace
    from cadillac.validate import run_validation, format_failures
    from cadillac.engine import _load_workspace
    ws_path = _resolve_workspace(args.workspace, os.getcwd())
    if not ws_path:
        print(f"Workspace not found: {args.workspace}")
        sys.exit(1)

    instruction = " ".join(args.instruction) if args.instruction else ""
    iter_rounds = getattr(args, "rounds", None)
    iter_read_budget = getattr(args, "read_budget", None)
    emitter, display = _make_emitter(use_rich=not args.plain)
    display.start()
    try:
        if getattr(args, "auto", False):
            max_iter = getattr(args, "max_iterations", 3)
            plan, _ = _load_workspace(ws_path)
            entry_point = plan.get("entry_point", "main.py") if plan else "main.py"
            for i in range(max_iter):
                results = run_validation(ws_path, entry_point)
                if not format_failures(results):
                    print(f"All tests pass after {i} iteration(s)!")
                    break
                print(f"--- Auto-iterate {i + 1}/{max_iter} ---")
                iterate(ws_path, cfg, instruction=instruction, emitter=emitter,
                        max_rounds=iter_rounds, read_budget_override=iter_read_budget)
        else:
            iterate(ws_path, cfg, instruction=instruction, emitter=emitter,
                    max_rounds=iter_rounds, read_budget_override=iter_read_budget)
    finally:
        display.stop()


def cmd_auto(args, cfg):
    """Build and auto-iterate until tests pass."""
    if args.workspace:
        workspace = os.path.abspath(args.workspace)
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        workspace = os.path.abspath(f"workspace-{ts}")

    emitter, display = _make_emitter(use_rich=not args.plain)
    display.start()
    try:
        build(args.task, workspace, cfg, max_iterations=args.max_iterations,
              emitter=emitter, parallel=getattr(args, "parallel", False))
    except KeyboardInterrupt:
        # SIGTERM / Ctrl+C: run()'s signal handler already saved the
        # partial-build phase history. Exit quietly without a traceback.
        print(f"\n[interrupted] partial build preserved in {workspace}", file=sys.stderr)
        sys.exit(130)
    finally:
        display.stop()


def cmd_resume(args, cfg):
    """Resume a crashed/interrupted build."""
    from cadillac.cli import _resolve_workspace
    ws_path = _resolve_workspace(args.workspace, os.getcwd())
    if not ws_path:
        print(f"Workspace not found: {args.workspace}")
        sys.exit(1)

    emitter, display = _make_emitter(use_rich=not args.plain)
    display.start()
    try:
        resume(ws_path, cfg, emitter=emitter)
    finally:
        display.stop()


def cmd_debug(args, cfg):
    """Focused debug on a workspace."""
    from cadillac.cli import _resolve_workspace
    ws_path = _resolve_workspace(args.workspace, os.getcwd())
    if not ws_path:
        print(f"Workspace not found: {args.workspace}")
        sys.exit(1)

    target = " ".join(args.target) if args.target else ""
    emitter, display = _make_emitter(use_rich=not args.plain)
    display.start()
    try:
        debug(ws_path, cfg, target=target, emitter=emitter)
    finally:
        display.stop()


def cmd_enhance(args, cfg):
    """Enhance an existing codebase."""
    workspace = os.path.abspath(args.workspace)
    if not os.path.isdir(workspace):
        print(f"Directory not found: {workspace}")
        sys.exit(1)

    task = " ".join(args.task)
    emitter, display = _make_emitter(use_rich=not args.plain)
    display.start()
    try:
        enhance(workspace, task, cfg, emitter=emitter)
    finally:
        display.stop()


def cmd_shell(args, cfg):
    """Enter interactive shell."""
    from cadillac.cli import interactive_shell
    use_rich = sys.stdout.isatty() and not getattr(args, 'plain', False)
    interactive_shell(cfg, os.getcwd(), use_rich=use_rich)


def cmd_dash(args, cfg):
    """Open the zero-service TUI dashboard."""
    from cadillac.dash import run as run_dash
    sys.exit(run_dash(root=getattr(args, "root", None)))


def cmd_improve(args, cfg):
    """Run the closed-loop self-improvement cycle."""
    from cadillac.improve.cli import run_improve_cycle
    stop = run_improve_cycle(
        cfg,
        cadillac_root=getattr(args, "root", None),
        max_iterations=getattr(args, "max_iterations", 30),
        quiet=getattr(args, "quiet", False),
    )
    print(f"\n[improve] STOPPED after {stop.iterations} iteration(s) "
          f"— reason: {stop.reason}")
    sys.exit(0 if stop.reason in ("saturated", "manual") else 1)


def main():
    parser = argparse.ArgumentParser(
        description="Cadillac — autonomous agent builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s new "Build a CLI todo app with SQLite storage"
  %(prog)s list
  %(prog)s iterate workspace-20260401 "fix auth"
  %(prog)s debug workspace-20260401 syntax
  %(prog)s                                          # interactive shell
""",
    )
    parser.add_argument("--api-url", default=os.getenv("CADILLAC_API_URL", "http://localhost:8000/v1"),
                        help="OpenAI-compatible API URL (env: CADILLAC_API_URL)")
    parser.add_argument("--api-key", default=os.getenv("CADILLAC_API_KEY"),
                        help="API key for authenticated endpoints (env: CADILLAC_API_KEY)")
    parser.add_argument("--model", default=os.getenv("CADILLAC_MODEL"),
                        help="Model ID, auto-detect if not set (env: CADILLAC_MODEL)")
    parser.add_argument("--context-window", type=int, default=int(os.getenv("CADILLAC_CONTEXT_WINDOW", "65536")),
                        help="Model context window size (env: CADILLAC_CONTEXT_WINDOW)")
    parser.add_argument("--max-context", type=int, default=int(os.getenv("CADILLAC_MAX_CONTEXT", "40000")),
                        help="Max tokens budget for code map context (env: CADILLAC_MAX_CONTEXT)")
    parser.add_argument("--context-auto", action="store_true",
                        help="Auto-detect context window from --api-url /v1/models and compute max-context with headroom for output + safety. Explicit --context-window / --max-context still win when set.")
    parser.add_argument("--rate", type=float, default=float(os.getenv("CADILLAC_RATE_LIMIT", "0.25")),
                        help="Max chat completions per second — protects the vLLM server from stampede. Default 0.25 (≈ one call every 4s). Pass 0 to disable. Env: CADILLAC_RATE_LIMIT. Only limits inference calls, not tool dispatches.")
    parser.add_argument("--stream", action="store_true", help="Stream LLM output")
    parser.add_argument("--plain", action="store_true", help="Disable Rich display (plain text output)")
    parser.add_argument("--full-spec", action="store_true",
                        help="Build all spec tiers (must + should + could). Default: must + should only — could-priority stories are skipped to bound build wall time.")

    subparsers = parser.add_subparsers(dest="command")

    # new
    p_new = subparsers.add_parser("new", help="Start a new build")
    p_new.add_argument("task", help="Task description")
    p_new.add_argument("--workspace", default=None, help="Workspace directory")

    # list
    subparsers.add_parser("list", help="List all workspaces")
    subparsers.add_parser("ls", help="List all workspaces")

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume a crashed/interrupted build")
    p_resume.add_argument("workspace", help="Workspace name or path")

    # iterate
    p_iter = subparsers.add_parser("iterate", help="Re-run BUILD on existing workspace")
    p_iter.add_argument("workspace", help="Workspace name or path")
    p_iter.add_argument("instruction", nargs="*", help="Optional instruction")
    p_iter.add_argument("--auto", action="store_true", help="Loop iterate until all tests pass")
    p_iter.add_argument("--max-iterations", type=int, default=3, help="Max iterate loops (with --auto)")
    p_iter.add_argument("--rounds", type=int, default=None, help="Max rounds per iterate (default: auto)")
    p_iter.add_argument("--read-budget", type=int, default=None, help="Max read_file calls (default: auto)")

    # auto
    p_auto = subparsers.add_parser("auto", help="Build and auto-iterate until tests pass")
    p_auto.add_argument("task", help="Task description")
    p_auto.add_argument("--workspace", default=None, help="Workspace directory")
    p_auto.add_argument("--max-iterations", type=int, default=3, help="Max iterate rounds")
    p_auto.add_argument("--parallel", action="store_true", help="Build independent modules in parallel")

    # debug
    p_debug = subparsers.add_parser("debug", help="Focused debug on a workspace")
    p_debug.add_argument("workspace", help="Workspace name or path")
    p_debug.add_argument("target", nargs="*", help="Target failure type")

    # enhance
    p_enhance = subparsers.add_parser("enhance", help="Enhance an existing project")
    p_enhance.add_argument("workspace", help="Project directory")
    p_enhance.add_argument("task", nargs="+", help="What to add or fix")

    # dash
    p_dash = subparsers.add_parser(
        "dash",
        help="Zero-service TUI dashboard over workspaces, memory, and phase history",
    )
    p_dash.add_argument("root", nargs="?", default=None,
                        help="Directory to scan for workspace-* dirs (default: cwd)")

    # improve — closed-loop self-improvement cycle
    p_improve = subparsers.add_parser(
        "improve",
        help="Run the audit→probe→correlate→propose→apply self-improvement "
             "cycle until the test matrix saturates or improvements stop coming",
    )
    p_improve.add_argument("--max-iterations", type=int, default=30,
                           help="Iteration cap (default 30)")
    p_improve.add_argument("--root", default=None,
                           help="Path to cadillac repo root (default: parent of this file)")
    p_improve.add_argument("--quiet", action="store_true",
                           help="Suppress per-event log output")

    args = parser.parse_args()

    if args.context_auto:
        # Only override when user didn't explicitly pass the flag on the CLI.
        # (argparse gives us the default even when the user passed it; inspect
        # sys.argv to distinguish — explicit user flags always win.)
        ctx_was_explicit = any(a.startswith("--context-window") for a in sys.argv)
        max_was_explicit = any(a.startswith("--max-context") for a in sys.argv)
        detected = _detect_context_window(args.api_url)
        if detected:
            if not ctx_was_explicit:
                args.context_window = detected
            if not max_was_explicit:
                args.max_context = compute_context_budget(args.context_window)
            print(
                f"[context-auto] api={args.api_url} max_model_len={detected} "
                f"→ context_window={args.context_window}, max_context={args.max_context}",
                file=sys.stderr,
            )
        else:
            print(
                f"[context-auto] detection failed against {args.api_url}; "
                f"falling back to context_window={args.context_window}, max_context={args.max_context}",
                file=sys.stderr,
            )

    cfg = Config(
        api_url=args.api_url,
        model=args.model,
        context_window=args.context_window,
        max_context_tokens=args.max_context,
        stream=args.stream,
        api_key=args.api_key,
        rate_limit=args.rate,
    )
    # Threaded into engine.run() for progressive-tier orchestration.
    cfg.full_spec = bool(getattr(args, "full_spec", False))

    if args.command == "new":
        cmd_new(args, cfg)
    elif args.command in ("list", "ls"):
        cmd_list(args, cfg)
    elif args.command == "resume":
        cmd_resume(args, cfg)
    elif args.command == "iterate":
        cmd_iterate(args, cfg)
    elif args.command == "auto":
        cmd_auto(args, cfg)
    elif args.command == "debug":
        cmd_debug(args, cfg)
    elif args.command == "enhance":
        cmd_enhance(args, cfg)
    elif args.command == "dash":
        cmd_dash(args, cfg)
    elif args.command == "improve":
        cmd_improve(args, cfg)
    elif args.command is None:
        # No subcommand — check for legacy positional task or enter shell
        # Support legacy: python3 -m cadillac "task description"
        remaining = sys.argv[1:]
        # Filter out known flags
        non_flag = [a for a in remaining if not a.startswith("--")]
        if non_flag and non_flag[0] not in ("new", "list", "ls", "iterate", "debug"):
            # Legacy one-shot mode
            task = non_flag[0]
            ts = time.strftime("%Y%m%d-%H%M%S")
            workspace = os.path.abspath(f"workspace-{ts}")
            emitter, display = _make_emitter(use_rich=not args.plain)
            display.start()
            try:
                run(task, workspace, cfg, emitter=emitter)
            finally:
                display.stop()
        else:
            cmd_shell(args, cfg)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
