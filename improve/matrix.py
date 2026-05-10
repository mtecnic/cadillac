"""Test matrix: 8 reference workloads the improve loop runs every iteration.

The matrix is the project's specification. A patch is "improving" iff the
matrix score goes up. Don't let the LLM mutate it — that's the cheap
trivialization path.

Each task wraps `engine.run()`. The runner captures validation outcomes,
retry counts, and elapsed time, then maps them to a TaskOutcome the
scoring module consumes.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from .scoring import TaskOutcome


@dataclass(frozen=True)
class MatrixTask:
    """One workload in the test matrix."""
    id: str
    prompt: str
    expected_lang: str  # for sanity-check that detection routes correctly
    expected_validation_keys: tuple[str, ...]  # subset of validation dict that must be True
    time_budget_s: float = 600.0
    notes: str = ""


# ── The matrix itself ──────────────────────────────────────────────────────
#
# 8 tasks covering the 8 language families Cadillac currently supports.
# Prompts are intentionally short — we want the LLM's PLAN phase to do the
# heavy lifting, not have everything spoon-fed. Each prompt is a real
# (small) task that exercises the full pipeline.

MATRIX: tuple[MatrixTask, ...] = (
    MatrixTask(
        id="py-cli",
        prompt=(
            "Build a Python CLI called wordcount that takes a file path "
            "argument and prints the count of unique words. Add a --top=N "
            "flag that prints the N most common words. argparse + pytest. "
            "Single file is fine if it stays under 200 lines."
        ),
        expected_lang="python",
        expected_validation_keys=("syntax", "imports", "static_names",
                                   "framework", "tests"),
        time_budget_s=600.0,
        notes="flat plan, smallest end-to-end Python target",
    ),
    MatrixTask(
        id="flask-rest",
        prompt=(
            "Build a Flask REST API for a TODO list using SQLite. Endpoints: "
            "GET /todos (list), POST /todos (create with {title}), "
            "PATCH /todos/<id> (mark done), DELETE /todos/<id>. pytest with "
            "Flask test client. CORS_ORIGINS env var with default 'http://"
            "localhost:5173'. Modular: backend/{api,storage,models}."
        ),
        expected_lang="python",
        expected_validation_keys=("syntax", "imports", "static_names",
                                   "framework", "tests"),
        time_budget_s=900.0,
        notes="modular Flask + sqlite, tests WIRING + contract phases",
    ),
    MatrixTask(
        id="react-spa",
        prompt=(
            "Build a React + Vite + TypeScript SPA: a markdown editor. "
            "Textarea on the left, rendered preview on the right. Save/load "
            "from localStorage. Use the marked library for rendering. "
            "vitest tests for at least one pure-logic helper. Keep flat "
            "(no need to be modular)."
        ),
        expected_lang="react",
        expected_validation_keys=("syntax", "framework", "run", "tests"),
        time_budget_s=600.0,
        notes="React/Vite SPA, tests TS toolchain end-to-end",
    ),
    MatrixTask(
        id="go-service",
        prompt=(
            "Build a Go HTTP service called pingpong. GET /ping returns "
            "{pong: true, count: N} where N is a global counter incremented "
            "atomically. POST /reset zeroes the counter. Modular: cmd/server "
            "+ internal/{api,counter}. go.mod, std net/http, sync/atomic. "
            "go test ./... must pass cleanly."
        ),
        expected_lang="go",
        expected_validation_keys=("syntax", "framework", "tests"),
        time_budget_s=600.0,
        notes="modular Go service, exercises compiled-family path",
    ),
    MatrixTask(
        id="rust-cli",
        prompt=(
            "Build a Rust CLI called linecount. Usage: linecount <file> [--non-empty]. "
            "Prints the line count; with --non-empty, skips blank lines. "
            "clap for args, anyhow for errors. Cargo.toml with edition=2021. "
            "Unit tests in #[cfg(test)] mod tests inside the same source files. "
            "DO NOT create nested src/<module>/src/lib.rs."
        ),
        expected_lang="rust",
        expected_validation_keys=("syntax", "framework", "tests"),
        time_budget_s=900.0,
        notes="flat Rust CLI, exercises the compiled-family + Rust prompt",
    ),
    MatrixTask(
        id="pytorch-flat",
        prompt=(
            "Build a tiny PyTorch project that trains a 2-layer MLP on "
            "synthetic XOR data and saves a checkpoint. train.py has --test "
            "flag that runs the canonical overfit-single-batch test and "
            "exits. requirements.txt lists torch (CPU is fine). Tests: "
            "shape check, gradient flow, overfit smoke."
        ),
        expected_lang="pytorch",
        expected_validation_keys=("syntax", "imports", "static_names", "tests"),
        time_budget_s=900.0,
        notes="exercises the pytorch overlay strategy",
    ),
    MatrixTask(
        id="wp-plugin",
        prompt=(
            "Build a WordPress plugin called HelloShortcode that adds a "
            "[hello-world] shortcode rendering 'Hello, <name>!' where name "
            "is a shortcode attribute. ABSPATH guard on every PHP file. "
            "All output through esc_html. Settings page in wp-admin to set "
            "a default name. Modular: includes/ + admin/. PHPUnit tests "
            "for the pure-logic name-formatting helper."
        ),
        expected_lang="wordpress",
        expected_validation_keys=("syntax", "framework"),
        time_budget_s=900.0,
        notes="exercises the new php family + WP security patterns",
    ),
    MatrixTask(
        id="browser-ext",
        prompt=(
            "Build a Chrome MV3 browser extension called WordHighlight. "
            "Content script highlights any occurrence of words from a "
            "user-configured list (stored in chrome.storage.sync). Options "
            "page (React) lets the user edit the list. Use @crxjs/vite-plugin@^2. "
            "vitest tests for the highlight-pure-logic helper, mocking "
            "chrome.* APIs."
        ),
        expected_lang="browser_extension",
        expected_validation_keys=("syntax", "framework"),
        time_budget_s=900.0,
        notes="exercises the new browser_extension strategy",
    ),
)


# ── Per-task runner ─────────────────────────────────────────────────────────


def run_matrix_task(task: MatrixTask, cfg, *, base_workspace_dir: str,
                    iteration: int) -> TaskOutcome:
    """Execute one matrix task end-to-end. Returns its TaskOutcome.

    Wraps engine.run() with a fresh workspace and a budget cap. Captures
    validation outcomes from the engine's progress dict and maps them
    onto the TaskOutcome shape the scorer expects.
    """
    # Late import — avoid circular dep at module load.
    from ..engine import run as engine_run, EventEmitter, default_print_handler
    from ..languages import detect_language

    workspace = os.path.join(
        base_workspace_dir,
        f"matrix-iter{iteration}-{task.id}-{int(time.time())}",
    )
    os.makedirs(workspace, exist_ok=True)

    # Capture events into a side channel so we can extract retry counts +
    # crash signals without parsing log text.
    events: list[dict] = []
    def collect(kind: str, **kw):
        events.append({"kind": kind, **kw})
        # Don't print every event during matrix runs — too noisy.

    emitter = EventEmitter()
    emitter.on(collect)

    t_start = time.time()
    crashed = False
    error_summary = ""
    try:
        # engine.run signature: (cfg, task, workspace, *, emitter, ...)
        # We run with a tight time budget; engine respects round budgets
        # internally so we just wait for it to finish.
        engine_run(
            cfg=cfg, task=task.prompt, workspace=workspace,
            emitter=emitter,
        )
    except Exception as e:
        crashed = True
        error_summary = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t_start

    # Extract results from the events
    validation_results: dict[str, bool] = {}
    retry_rounds = 0
    for e in events:
        if e["kind"] == "validation" and "results" in e:
            for r in e["results"]:
                validation_results[r["name"]] = r["passed"]
        if e["kind"] == "log":
            msg = e.get("msg", "")
            if "retry" in msg.lower() and "/" in msg:
                # Crude: count occurrences of "retry N/M" in logs
                retry_rounds += 1

    # Score against expected_validation_keys
    expected = set(task.expected_validation_keys)
    passed = sum(1 for k in expected if validation_results.get(k, False))
    total = len(expected)

    return TaskOutcome(
        task_id=task.id,
        passed_checks=passed,
        total_checks=total,
        retry_rounds=retry_rounds,
        elapsed_s=elapsed,
        crashed=crashed,
        error_summary=error_summary,
    )
