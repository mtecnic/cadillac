"""Tool definitions, implementations, security, and dispatch."""

import json
import os
import re
import subprocess
from difflib import SequenceMatcher

from .manifest import FileManifest


# ── Input coercion ────────────────────────────────────────────────────────────

def _coerce(val, expected_type, default=None):
    """Coerce LLM-provided value to expected type, return default if impossible."""
    if isinstance(val, expected_type):
        return val
    if val is None:
        return default
    try:
        return expected_type(val)
    except (TypeError, ValueError):
        return default


def _coerce_int(val) -> int | None:
    """Coerce LLM-provided value to int. Handles '22.0', '170, end_line=182', etc."""
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return int(float(val))
        except ValueError:
            pass
        m = re.match(r'(\d+)', val)
        if m:
            return int(m.group(1))
    return None


# ── Read-limit budgeting ─────────────────────────────────────────────────────

def compute_read_limits(max_context_tokens: int | None = None) -> tuple[int, int]:
    """Derive (max_chars, max_lines) for read_file from the model's context budget.

    Rationale: a 16K-char hard cap on read_file is fine for a 40K-token window
    but silently truncates on a 131K-token model that could easily absorb the
    whole file. Both caps scale with ~15% of the context budget.

    Rough conversions: 4 chars per token (English+code average), 8 tokens per
    line (typical code). So 15% of a 131K window gives:
      - chars: 131072 * 4 * 0.15 = 78,643
      - lines: 131072 * 0.15 / 8  = 2,457

    Floors ((8000, 1000)) prevent tiny-context regressions; ceilings
    ((200000, 40000)) bound memory on pathological files.
    Falls back to legacy (16000, 5000) when context is unknown.
    """
    if not max_context_tokens or max_context_tokens <= 0:
        return (16000, 5000)
    max_chars = int(max_context_tokens * 4 * 0.15)
    max_lines = int(max_context_tokens * 0.15 / 8)
    return (
        max(8000, min(200_000, max_chars)),
        max(1000, min(40_000, max_lines)),
    )


# ── Adaptive timeouts ────────────────────────────────────────────────────────
#
# Motivation: a 90-second pytest suite gets killed at `timeout=60`; a 10-second
# lint check with a hang symptom burns the full 30. Both are fixable if we just
# observe past runs and pick a timeout that actually fits THIS workspace's
# commands.
#
# Strategy:
#   - On every `run_command`, record (cmd_kind, elapsed_ms, exit) in
#     workspace/.cadillac/cmd_history.jsonl (bounded to ~500 entries).
#   - On next call with the same cmd_kind, timeout = max(baseline, p95*2.5)
#     clamped to a ceiling. Baseline covers the cold-workspace case.
#
# Same helper is used by validate.py `_run()` so every timeout in the harness
# is derived from history, not hardcoded literals.

_CMD_HISTORY_FILE = ".cadillac/cmd_history.jsonl"
_CMD_HISTORY_WINDOW = 20      # consider the last N matching entries for stats
_CMD_HISTORY_CEILING = 600    # seconds — never wait more than this


def _cmd_kind(command: str) -> str:
    """Classify a shell command for timeout scaling. Unwraps `npx <runner>` /
    `npm test` / `npm run <script>` so the runner gets its own bucket."""
    if not command or not command.strip():
        return "unknown"
    parts = command.strip().split()
    tok = os.path.basename(parts[0])
    if tok in ("npx", "npm", "yarn", "pnpm") and len(parts) > 1:
        second = parts[1].split("/")[-1]
        if second in ("jest", "vitest", "tsc", "eslint", "prettier", "vite",
                      "ts-node", "tsx", "mocha", "ava", "tap"):
            return second
        if tok == "npm" and second == "test":
            return "npm-test"
        if tok == "npm" and second in ("run", "start"):
            return f"npm-{second}"
    return tok


def _record_cmd_history(workspace: str, kind: str, elapsed_ms: int, exit_code: int) -> None:
    """Append one command outcome to the rolling history file. Best-effort; a
    failed write does NOT fail the user's command."""
    if not workspace:
        return
    try:
        path = os.path.join(workspace, _CMD_HISTORY_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        entry = {"cmd": kind, "elapsed_ms": int(elapsed_ms), "exit": int(exit_code)}
        with open(path, "a") as f:
            f.write(__import__("json").dumps(entry) + "\n")
    except Exception:
        pass


def _adaptive_timeout(command: str, workspace: str, baseline: int = 60,
                     ceiling: int = _CMD_HISTORY_CEILING) -> int:
    """Derive a timeout in seconds for this command, based on past elapsed.

    Formula: `max(baseline, p95(recent_matching) / 1000 * 2.5)`, clamped to
    `ceiling`. Returns `baseline` when no history exists (cold workspace) so
    first-time behavior is identical to the old hardcoded default.

    Safe against filesystem errors — never raises; worst case it degrades to
    returning `baseline`.
    """
    if not workspace:
        return baseline
    try:
        import json as _json
        path = os.path.join(workspace, _CMD_HISTORY_FILE)
        if not os.path.exists(path):
            return baseline
        kind = _cmd_kind(command)
        past = []
        with open(path) as f:
            for line in f:
                try:
                    entry = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if entry.get("cmd") == kind:
                    past.append(int(entry.get("elapsed_ms", 0)))
        if not past:
            return baseline
        recent = past[-_CMD_HISTORY_WINDOW:]
        recent.sort()
        # p95 index; with few samples just take the max
        idx = min(len(recent) - 1, int(len(recent) * 0.95))
        p95_ms = recent[idx]
        scaled = int(p95_ms / 1000 * 2.5)  # ms → s, 2.5× safety factor
        return max(baseline, min(ceiling, scaled))
    except Exception:
        return baseline


# ── PEP 668 (Ubuntu 24.04 / Debian 12+ externally-managed-environment) ──────
#
# System Python installs on recent Debian-family distros ship an
# `EXTERNALLY-MANAGED` marker that makes `pip install foo` error out with
# guidance to use apt / a venv / pipx. The LLM doesn't know which distro the
# harness is on and keeps re-running `pip install` hoping it works.
#
# Fix: detect the marker once; if present, transparently add
# `--break-system-packages` to the LLM's `pip install` commands (when they
# don't already opt into user-scope, venv-scope, or explicitly request this
# flag). One less category of stuck-builds.

_PEP668_DETECTED: bool | None = None


def _host_is_pep668() -> bool:
    """Return True if system Python has the EXTERNALLY-MANAGED marker.
    Cached — the host doesn't change between runs."""
    global _PEP668_DETECTED
    if _PEP668_DETECTED is not None:
        return _PEP668_DETECTED
    try:
        import glob
        marker_patterns = [
            "/usr/lib/python3*/EXTERNALLY-MANAGED",
            "/usr/lib/python3.*/EXTERNALLY-MANAGED",
        ]
        for pat in marker_patterns:
            if glob.glob(pat):
                _PEP668_DETECTED = True
                return True
        _PEP668_DETECTED = False
    except Exception:
        _PEP668_DETECTED = False
    return _PEP668_DETECTED


# Matches `pip install`, `pip3 install`, `python3 -m pip install`, `pip-<ver> install`
# ONLY when at the start of a shell statement (string start or after ;, &&, ||, |,
# or newline). Prevents false positives on phrases like `ls pip install README`.
_PIP_INSTALL_RE = re.compile(
    r"(?:(?<=^)|(?<=;)|(?<=&&)|(?<=\|\|)|(?<=\|)|(?<=\n))\s*"
    r"(?:python3?(?:\.\d+)?\s+-m\s+)?pip3?(?:[-.]?\d+(?:\.\d+)?)?\s+install\b"
)
# Opt-outs that make --break-system-packages unnecessary / redundant.
_PEP668_OPT_OUT_FLAGS = ("--user", "--target", "--prefix", "--root",
                         "--break-system-packages")


def _rewrite_pip_for_pep668(command: str) -> str:
    """Append `--break-system-packages` to pip install invocations when the
    host has the marker and the user hasn't already chosen a scope. No-op on
    non-PEP-668 hosts.

    In-venv runs (VIRTUAL_ENV env var) short-circuit: pip in a venv doesn't
    hit the system marker. Same for pipx / conda environments."""
    if not command or not _host_is_pep668():
        return command
    if os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX"):
        return command
    if not _PIP_INSTALL_RE.search(command):
        return command
    if any(flag in command for flag in _PEP668_OPT_OUT_FLAGS):
        return command
    # Insert the flag right after the first `install` keyword so quoting
    # and downstream pipes remain intact. Only replace the first occurrence.
    return _PIP_INSTALL_RE.sub(
        lambda m: m.group(0) + " --break-system-packages", command, count=1,
    )


# ── Security ─────────────────────────────────────────────────────────────────

ALLOWED_COMMANDS = {
    "python3", "pip3", "pytest", "ruff", "mypy", "black",
    "node", "npm", "npx", "bun",
    "tsc", "tsx", "eslint", "jest", "vitest", "prettier",
    "yarn", "pnpm", "vite", "ng",
    "ls", "cat", "head", "tail", "wc", "find", "grep", "sort", "uniq",
    "mkdir", "rm", "cp", "mv", "touch", "chmod", "diff",
    "echo", "env", "which", "file", "stat",
    "cd", "timeout", "pwd", "lsof", "ss", "netstat",
    "true", "false", "test",
}

BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+~",
    r"sudo\s+",
    r"curl.*\|\s*(ba)?sh",
    r"wget.*\|\s*(ba)?sh",
    r">\s*/etc/",
    r">\s*/home/\w+/\.",
    r"docker\s+",
    r"systemctl\s+",
    r"ssh\s+",
    r"\bnc\b\s+-",
    r"chmod\s+777",
]


# Config files the LLM corrupts repeatedly (package.json "type":"module" loop, tsconfig rootDir loop).
# Post-SCAFFOLD, these are only writable through add_dep() or the harness's own safeguard helpers.
PROTECTED_CONFIG_PATHS = frozenset({
    "package.json", "tsconfig.json",
    "jest.config.js", "jest.config.ts",
    "vite.config.ts", "vite.config.js",
    "vitest.config.ts", "vitest.config.js",
})

_CONFIG_WRITE_SHELL_RE = re.compile(
    r"(?:>\s*|>>\s*|tee\s+(?:-\S*\s+)?|sed\s+-i\S*\s+\S+\s+)"
    r"[\"']?\S*?(" + "|".join(re.escape(p) for p in PROTECTED_CONFIG_PATHS) + r")"
)


_FAILURE_MARKERS = re.compile(
    r"(?m)^\s*(?:FAIL\b|✗|error TS\d+|AssertionError|Traceback|"
    r"SyntaxError|TypeError|ReferenceError|●\s)"
)

_TEST_RUNNER_COMMANDS = frozenset({
    "jest", "vitest", "pytest", "tsc", "npm", "npx", "yarn", "pnpm", "node",
})


def extract_failure_text(result: dict, command: str) -> str:
    """Return failure-bearing output from a run_command result, or empty string.

    Handles three cases that a naive stderr check misses:
      1. exit_code != 0 with output in stdout (vitest/jest/pytest)
      2. exit_code == 0 but runner printed FAIL / error TS lines (edge cases)
      3. Both stderr and stdout present — combine them so ErrorTracker sees full context
    """
    if not isinstance(result, dict):
        return ""
    stderr = (result.get("stderr") or "").strip()
    stdout = (result.get("stdout") or "").strip()
    exit_code = result.get("exit_code", 0)
    if stderr and stdout:
        combined = (stderr + "\n" + stdout).strip()
    else:
        combined = stderr or stdout
    if exit_code != 0:
        return combined
    first = command.strip().split()[0] if command and command.strip() else ""
    first = os.path.basename(first)
    if first in _TEST_RUNNER_COMMANDS and combined and _FAILURE_MARKERS.search(combined):
        return combined
    return ""


def validate_path(path: str, workspace: str) -> bool:
    """Ensure path resolves within workspace."""
    full = os.path.realpath(os.path.join(workspace, path))
    ws_real = os.path.realpath(workspace)
    return full.startswith(ws_real + os.sep) or full == ws_real


def _split_command_parts(command: str) -> list[str]:
    """Split command on &&, ||, ;, | while respecting quotes and backslash escapes."""
    parts = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        c = command[i]
        # Backslash escapes next char (outside single quotes)
        if c == "\\" and not in_single and i + 1 < len(command):
            current.append(c)
            current.append(command[i + 1])
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if command[i:i+2] in ("&&", "||"):
                parts.append("".join(current).strip())
                current = []
                i += 2
                continue
            elif c in (";", "|"):
                parts.append("".join(current).strip())
                current = []
                i += 1
                continue
        current.append(c)
        i += 1
    remaining = "".join(current).strip()
    if remaining:
        parts.append(remaining)
    return parts


def validate_command(command: str) -> tuple[bool, str]:
    """Check if a command is allowed."""
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            return False, f"Blocked: matches dangerous pattern"
    # Block redirections to absolute paths (prevent writes outside workspace)
    redirect_targets = re.findall(r'[12]?>+\s*(\S+)', command)
    for target in redirect_targets:
        if os.path.isabs(target):
            return False, f"Blocked: redirect to absolute path '{target}'"
    parts = _split_command_parts(command)
    for part in parts:
        # Strip redirections like 2>&1, 2>/dev/null, >/file
        part = re.sub(r'\d*>>&?\s*\S+', '', part)  # 2>/dev/null, >>/file
        part = re.sub(r'\d*>&?\d+', '', part)       # 2>&1
        part = part.strip()
        tokens = part.split()
        if not tokens:
            continue
        cmd = os.path.basename(tokens[0])
        if cmd not in ALLOWED_COMMANDS:
            return False, f"Command '{cmd}' not in allowlist. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"
    return True, ""


# ── Tool definitions (OpenAI function-calling schema) ────────────────────────

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write full content to a file. Creates parent dirs. Use for new files or complete rewrites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'src/app.py'"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Surgical find-and-replace. Each edit: {'old': 'exact text', 'new': 'replacement'}. Has fuzzy matching for whitespace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to edit"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string", "description": "Exact text to find"},
                                "new": {"type": "string", "description": "Replacement text"},
                            },
                            "required": ["old", "new"],
                        },
                        "description": "List of find-and-replace operations",
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "line_edit",
            "description": "Replace lines by number range. Line numbers are shown in the code map.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "First line (1-based)"},
                    "end_line": {"type": "integer", "description": "Last line (inclusive)"},
                    "new_content": {"type": "string", "description": "Replacement content"},
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents. Do NOT read files you just wrote.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "Optional first line (1-based). Aliases: line_start."},
                    "end_line": {"type": "integer", "description": "Optional last line (inclusive). Aliases: line_end."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace. Use python3, pip3. Timeout: 60s.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative dir path, '.' for root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Grep for a pattern in workspace files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "file_glob": {"type": "string", "description": "Optional glob, e.g. '*.py'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_test",
            "description": "Write a test file. Path must start with 'test_', end with .test.ts/.spec.ts, or be in tests/ or __tests__/ dir. Tests can be freely rewritten.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Test file path, e.g. 'test_main.py' or '__tests__/core.test.ts'"},
                    "content": {"type": "string", "description": "Full test file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_status",
            "description": "Get current build progress: phase, round, files written, validation state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_lesson",
            "description": (
                "Record a within-build note for yourself. Survives phase transitions and "
                "message compression — re-injected into your system prompt every round. "
                "Use when you discover a working pattern, hit a dead end you should NOT retry, "
                "or need to remind yourself of a constraint for later phases. Cap: 200 chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["tried_failed", "working_pattern", "reminder"],
                        "description": (
                            "tried_failed = a thing that did not work, do not retry. "
                            "working_pattern = a confirmed-good approach. "
                            "reminder = note for the next phase."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Concise note, max 200 chars. Be specific (file/symbol names).",
                    },
                },
                "required": ["category", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_dep",
            "description": (
                "Add an npm dependency to package.json safely. "
                "package.json is protected post-scaffold — this is the only way to edit it. "
                "Harness applies the change, preserves other fields, and never re-adds 'type: module'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Package name, e.g. 'zod' or '@types/node'"},
                    "version": {"type": "string", "description": "Version range, e.g. '^3.22.0'. Default 'latest' if omitted."},
                    "dev": {"type": "boolean", "description": "True for devDependencies."},
                },
                "required": ["name"],
            },
        },
    },
]


# ── Tool implementations ─────────────────────────────────────────────────────

class ToolExecutor:
    """Stateful tool executor bound to a workspace and manifest."""

    PARALLEL_SAFE = {"write_file", "read_file", "edit_file", "line_edit",
                     "list_files", "search_files", "delete_file", "write_test",
                     "check_status", "note_lesson", "add_dep"}
    READ_LIMIT = 16000  # legacy default (chars); used when context_budget is not provided

    def __init__(self, workspace: str, manifest: FileManifest, progress_fn=None,
                 context_budget: int | None = None):
        self.workspace = workspace
        self.manifest = manifest
        self.progress_fn = progress_fn  # callable returning progress context string
        self._rewrites_allowed = 0
        self.build_mode = False  # When True: full file reads, unlimited rewrites
        self.scratch = None  # Set by engine to a Scratch instance for this build/module
        self.phase_name = "?"  # Set by engine per-phase
        self.round_num = 0     # Set by engine per-round
        # True only during SCAFFOLD. Engine flips to False on entry to BUILD/INTEGRATE/VALIDATE/iterate.
        self.config_writes_allowed = True
        # Used by add_dep() and harness safeguard helpers to bypass the guard for a single call.
        self._config_bypass = False
        # Context-proportional read caps — silently-truncating a 16K-char read when
        # the model has 111K tokens of room is the worst kind of bug. Derive from
        # cfg.max_context_tokens at creation time.
        self._read_chars, self._read_lines = compute_read_limits(context_budget)

    def grant_rewrites(self, n: int = 3):
        if self._rewrites_allowed <= 0:
            self._rewrites_allowed = n

    def normalize_path(self, path: str) -> str:
        """Strip /testbed/, workspace prefix, or other absolute prefixes models add."""
        if path.startswith("/testbed/"):
            path = path[len("/testbed/"):]
        # Strip workspace prefix if model used absolute path
        ws = self.workspace.rstrip("/") + "/"
        if path.startswith(ws):
            path = path[len(ws):]
        return path.lstrip("/")

    def _full_path(self, path: str) -> str:
        return os.path.join(self.workspace, self.normalize_path(path))

    def _check_path(self, path: str) -> str | None:
        path = self.normalize_path(path)
        if not validate_path(path, self.workspace):
            return f"Path '{path}' escapes workspace"
        return None

    def _config_guard(self, path: str) -> str | None:
        """Block direct writes to protected config files post-scaffold.

        Returns an error message (to be returned to the LLM) or None if the write is allowed.
        add_dep() and harness safeguard helpers set self._config_bypass=True for a single call.
        """
        if self._config_bypass or self.config_writes_allowed:
            return None
        if os.path.basename(path) in PROTECTED_CONFIG_PATHS:
            return (
                f"BLOCKED: {path} is protected post-scaffold. "
                "Use add_dep(name=..., version=..., dev=False) to add npm dependencies. "
                "tsconfig.json and *.config.* are frozen — the harness enforces safe defaults. "
                "If you think a config change is truly required, call "
                "note_lesson('reminder', 'need <config change> because <reason>') and proceed with code."
            )
        return None

    # ── Individual tool methods ──

    def write_file(self, path: str, content: str) -> dict:
        path = _coerce(path, str)
        content = _coerce(content, str, "")
        if not path:
            return {"error": "path is required"}
        if len(content) > 500_000:
            return {"error": f"content too large ({len(content)} bytes, max 500KB)"}
        path = self.normalize_path(path)
        if err := self._check_path(path):
            return {"error": err}
        if err := self._scope_check(path):
            return {"error": err}
        if err := self._config_guard(path):
            return {"error": err}
        full = self._full_path(path)
        existing = self.manifest.files.get(path)
        if existing and not self.build_mode:
            if self._rewrites_allowed <= 0:
                return {
                    "error": "BLOCKED: Test the code with run_command before rewriting. Use edit_file for surgical fixes.",
                    "path": path, "existing_lines": existing["lines"], "existing_version": existing["version"],
                }
            self._rewrites_allowed -= 1
        os.makedirs(os.path.dirname(full) or self.workspace, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        lines = content.count("\n") + 1
        self.manifest.record(path, content)
        return {"status": "ok", "path": path, "lines": lines, "bytes": len(content)}

    def edit_file(self, path: str, edits: list) -> dict:
        path = _coerce(path, str)
        if not path:
            return {"error": "path is required"}
        path = self.normalize_path(path)
        if err := self._check_path(path):
            return {"error": err}
        if err := self._scope_check(path):
            return {"error": err}
        if err := self._config_guard(path):
            return {"error": err}
        if not isinstance(edits, list):
            return {"error": "edits must be a list of {old, new} dicts"}
        if len(edits) > 50:
            return {"error": f"too many edits ({len(edits)}, max 50)"}
        full = self._full_path(path)
        try:
            with open(full) as f:
                content = f.read()
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}

        applied = 0
        failed = []
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                failed.append(f"edit {i}: must be a dict with 'old' and 'new' keys, got {type(edit).__name__}")
                continue
            old = _coerce(edit.get("old"), str, "")
            new = _coerce(edit.get("new"), str, "")
            if not old:
                failed.append(f"edit {i}: 'old' must be a non-empty string")
                continue
            if old in content:
                content = content.replace(old, new, 1)
                applied += 1
            else:
                # Fuzzy: strip trailing whitespace
                old_s = "\n".join(l.rstrip() for l in old.split("\n"))
                content_s = "\n".join(l.rstrip() for l in content.split("\n"))
                if old_s in content_s:
                    content = content_s.replace(old_s, new, 1)
                    applied += 1
                else:
                    nearby = self._find_nearby(content, old)
                    fail_msg = f"edit {i}: old text not found"
                    if nearby:
                        fail_msg += f"\n  nearby:\n{nearby}"
                    failed.append(fail_msg)

        if applied > 0:
            with open(full, "w") as f:
                f.write(content)
            self.manifest.record(path, content)

        result = {"status": "ok", "path": path, "applied": applied, "lines": content.count("\n") + 1}
        if failed:
            result["failed"] = failed
        return result

    def line_edit(self, path: str, start_line: int, end_line: int, new_content: str) -> dict:
        path = _coerce(path, str)
        start_line = _coerce(start_line, int)
        end_line = _coerce(end_line, int)
        new_content = _coerce(new_content, str, "")
        if not path:
            return {"error": "path is required"}
        if start_line is None or end_line is None:
            return {"error": "start_line and end_line are required integers"}
        path = self.normalize_path(path)
        if err := self._check_path(path):
            return {"error": err}
        if err := self._scope_check(path):
            return {"error": err}
        if err := self._config_guard(path):
            return {"error": err}
        full = self._full_path(path)
        try:
            with open(full) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}

        total = len(lines)
        if start_line < 1 or end_line < start_line or start_line > total:
            return {"error": f"Invalid range {start_line}-{end_line} (file has {total} lines)"}

        end_line = min(end_line, total)
        new_lines = [l + "\n" for l in new_content.split("\n")]
        if lines and not lines[-1].endswith("\n") and end_line == total and new_lines:
            new_lines[-1] = new_lines[-1].rstrip("\n")
        lines[start_line - 1:end_line] = new_lines
        content = "".join(lines)

        with open(full, "w") as f:
            f.write(content)
        self.manifest.record(path, content)
        return {"status": "ok", "path": path, "replaced": f"{start_line}-{end_line}", "new_total": len(lines)}

    def read_file(self, path: str, line_start: int = None, line_end: int = None,
                  offset: int = None, limit: int = None,
                  start_line: int = None, end_line: int = None) -> dict:
        path = _coerce(path, str)
        if not path:
            return {"error": "path is required"}
        path = self.normalize_path(path)
        # Accept both naming conventions. `start_line`/`end_line` matches our
        # own `line_edit` tool; some models (larger Qwens) prefer this form.
        # Precedence when both are given: `start_line`/`end_line` win because
        # that's the form matching the rest of our toolset.
        if start_line is not None:
            line_start = _coerce_int(start_line)
        else:
            line_start = _coerce_int(line_start)
        if end_line is not None:
            line_end = _coerce_int(end_line)
        else:
            line_end = _coerce_int(line_end)
        # Also accept offset/limit as aliases (other models prefer these).
        if offset is not None and line_start is None:
            line_start = _coerce_int(offset) or 1
        if limit is not None and line_end is None and line_start is not None:
            limit_int = _coerce(limit, int, 100)
            limit_int = min(max(1, limit_int), self._read_lines)
            line_end = line_start + limit_int - 1
        if err := self._check_path(path):
            return {"error": err}
        # Guard: return summary for recently written files (skip in build_mode — LLM needs full content to debug)
        if path in self.manifest.files and not line_start and not line_end and not self.build_mode:
            info = self.manifest.files[path]
            return {
                "path": path,
                "note": "You wrote this file. Use edit_file to fix issues.",
                "lines": info["lines"], "bytes": info["bytes"], "version": info["version"],
                "structure": info["summary"],
            }
        full = self._full_path(path)
        try:
            with open(full) as f:
                if line_start or line_end:
                    lines = f.readlines()
                    start = max(0, (line_start or 1) - 1)
                    end = min(len(lines), line_end or len(lines))
                    content = "".join(lines[start:end])
                    return {"path": path, "lines": f"{start+1}-{end}", "total_lines": len(lines),
                            "content": content[:self._read_chars]}
                else:
                    content = f.read(self._read_chars)
                    total = content.count("\n") + 1
                    result = {"path": path, "total_lines": total, "content": content}
                    if os.path.getsize(full) > self._read_chars:
                        result["truncated"] = True
                    return result
        except Exception as e:
            return {"error": str(e)}

    def run_command(self, command: str) -> dict:
        command = _coerce(command, str)
        if not command:
            return {"error": "command is required"}
        if len(command) > 10_000:
            return {"error": f"command too long ({len(command)} chars, max 10KB)"}
        # Strip "cd <path> &&" prefix — usually LLM hallucinations (/testbed,
        # absolute paths, non-existent dirs). EXCEPT when <path> is a real
        # subdirectory of the workspace: those are legitimate chdir-into-
        # frontend/backend patterns that full-stack builds need (else
        # `cd frontend && npm install` becomes `npm install` at workspace
        # root, which errors ENOENT on no root package.json).
        m = re.match(r'^cd\s+(\S+)\s*&&\s*(.*)$', command, re.DOTALL)
        if m:
            target = m.group(1)
            sub_path = os.path.join(self.workspace, target)
            if (not target.startswith(("/", "~"))
                    and os.path.isdir(sub_path)
                    and validate_path(target, self.workspace)):
                # Legitimate workspace subdir — keep the cd so the command
                # runs there. Use shell=True semantics (already set below).
                pass
            else:
                command = m.group(2)
        # Rewrite /testbed references — Qwen models hallucinate this path
        command = command.replace('/testbed/', './')
        command = re.sub(r'/testbed\b', '.', command)
        # Ubuntu 24.04 / Debian 12+ PEP 668: pip install against system
        # Python errors out; auto-add --break-system-packages when needed.
        command = _rewrite_pip_for_pep668(command)
        allowed, reason = validate_command(command)
        if not allowed:
            return {"error": reason}
        # Post-scaffold, block shell writes to protected config files.
        if not self.config_writes_allowed and _CONFIG_WRITE_SHELL_RE.search(command):
            return {
                "exit_code": 1, "stdout": "",
                "stderr": (
                    "Shell write to a protected config file blocked. "
                    "Use add_dep(name, version, dev) for package.json. "
                    "Other configs (tsconfig, *.config.*) are frozen post-scaffold."
                ),
            }
        self.grant_rewrites(3)
        # Adaptive timeout — scales with past runs of same cmd_kind in this workspace.
        # Cold workspace: baseline=60 (matches historical default).
        effective_timeout = _adaptive_timeout(command, self.workspace, baseline=60)
        try:
            import time as _time
            t0 = _time.monotonic()
            result = subprocess.run(
                command, shell=True, cwd=self.workspace,
                capture_output=True, text=True, timeout=effective_timeout,
            )
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            _record_cmd_history(self.workspace, _cmd_kind(command),
                                elapsed_ms, result.returncode)
            stdout = result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout
            stderr = result.stderr[-1500:] if len(result.stderr) > 1500 else result.stderr
            return {"exit_code": result.returncode, "stdout": stdout, "stderr": stderr}
        except subprocess.TimeoutExpired:
            # Record the timeout so the NEXT run of this kind gets more budget.
            _record_cmd_history(self.workspace, _cmd_kind(command),
                                effective_timeout * 1000, 124)
            return {"error": f"Command timed out after {effective_timeout}s"}
        except Exception as e:
            return {"error": str(e)}

    def list_files(self, path: str) -> dict:
        path = _coerce(path, str, ".")
        path = self.normalize_path(path)
        if err := self._check_path(path):
            return {"error": err}
        full = self._full_path(path)
        try:
            result = subprocess.run(
                ["find", ".", "-type", "f", "-not", "-path", "./.git/*",
                 "-not", "-path", "./__pycache__/*", "-not", "-path", "./node_modules/*",
                 "-not", "-path", "./.venv/*"],
                cwd=full, capture_output=True, text=True, timeout=5,
            )
            files = sorted(result.stdout.strip().split("\n")) if result.stdout.strip() else []
            return {"path": path, "files": files, "count": len(files)}
        except Exception as e:
            return {"error": str(e)}

    def search_files(self, pattern: str, file_glob: str = None) -> dict:
        pattern = _coerce(pattern, str)
        file_glob = _coerce(file_glob, str)
        if not pattern:
            return {"error": "pattern is required"}
        if len(pattern) > 1000:
            return {"error": "pattern too long (max 1000 chars)"}
        cmd = ["grep", "-rn", f"--include={file_glob}", pattern, "."] if file_glob else ["grep", "-rn", pattern, "."]
        try:
            result = subprocess.run(cmd, cwd=self.workspace, capture_output=True, text=True, timeout=10)
            matches = result.stdout.strip().split("\n")[:30] if result.stdout.strip() else []
            return {"pattern": pattern, "matches": matches, "count": len(matches)}
        except Exception as e:
            return {"error": str(e)}

    def delete_file(self, path: str) -> dict:
        path = _coerce(path, str)
        if not path:
            return {"error": "path is required"}
        path = self.normalize_path(path)
        if err := self._check_path(path):
            return {"error": err}
        if err := self._scope_check(path):
            return {"error": err}
        full = self._full_path(path)
        try:
            os.remove(full)
            self.manifest.remove(path)
            return {"status": "deleted", "path": path}
        except Exception as e:
            return {"error": str(e)}

    def write_test(self, path: str, content: str) -> dict:
        """Write a test file — no rewrite guard, but must be a test file."""
        path = _coerce(path, str)
        content = _coerce(content, str, "")
        if not path:
            return {"error": "path is required"}
        if len(content) > 500_000:
            return {"error": f"content too large ({len(content)} bytes, max 500KB)"}
        path = self.normalize_path(path)
        basename = os.path.basename(path)
        is_test_file = (
            basename.startswith("test_")
            or basename.endswith("_test.py")
            or basename.endswith(".test.ts") or basename.endswith(".test.js")
            or basename.endswith(".spec.ts") or basename.endswith(".spec.js")
            or path.startswith("tests/") or path.startswith("__tests__/")
            or "/__tests__/" in path or "/tests/" in path
        )
        if not is_test_file:
            return {"error": "Test file path must start with 'test_', end with .test.ts/.spec.ts, or be in tests/ or __tests__/ directory"}
        if err := self._check_path(path):
            return {"error": err}
        if err := self._scope_check(path):
            return {"error": err}
        full = self._full_path(path)
        os.makedirs(os.path.dirname(full) or self.workspace, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        lines = content.count("\n") + 1
        self.manifest.record(path, content)
        return {"status": "ok", "path": path, "lines": lines, "bytes": len(content)}

    def check_status(self) -> dict:
        try:
            if self.progress_fn:
                return {"status": self.progress_fn()}
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return {"status": "No progress tracking active"}

    def note_lesson(self, category: str, content: str) -> dict:
        """Append a within-build note. Persists across phases via .cadillac/scratch.md."""
        category = _coerce(category, str, "")
        content = _coerce(content, str, "")
        if self.scratch is None:
            return {"error": "scratch not initialized — note_lesson unavailable in this context"}
        result = self.scratch.append(
            category=category, content=content,
            phase=self.phase_name, round_num=self.round_num,
        )
        return {"status": result, "category": category}

    def add_dep(self, name: str, version: str = "latest", dev: bool = False) -> dict:
        """Safely add an npm dependency to package.json.

        The only sanctioned way to touch package.json post-scaffold. Preserves all other
        fields and never re-introduces "type": "module". Also consults the inspector's
        version policy (APPROVED_VERSIONS) and coerces unpinned/host-unsafe ranges —
        e.g. vitest:* → vitest:^1 for Node 18 compatibility.
        """
        name = _coerce(name, str, "")
        version = _coerce(version, str, "latest") or "latest"
        if not name:
            return {"error": "name is required"}
        if not re.match(r"^(@[\w.-]+/)?[\w.-]+$", name):
            return {"error": f"invalid package name: {name!r}"}
        # Policy gate — catches unsafe versions at the source.
        from .inspector import coerce_version
        coerced, was_coerced = coerce_version(name, version)
        effective_version = coerced
        import json as _json
        pkg_path = os.path.join(self.workspace, "package.json")
        if not os.path.exists(pkg_path):
            return {"error": "package.json not found — not a Node project or scaffold incomplete"}
        try:
            with open(pkg_path) as f:
                pkg = _json.load(f)
        except (OSError, _json.JSONDecodeError) as e:
            return {"error": f"could not parse package.json: {e}"}
        key = "devDependencies" if dev else "dependencies"
        deps = pkg.setdefault(key, {})
        deps[name] = effective_version
        # Harden against the recurring "type": "module" corruption — but ONLY
        # when the project is a CJS/Jest setup. Vite, Vitest, and SPA
        # frameworks (React/Vue/Angular via Vite) REQUIRE `type: module` to
        # silence the "CJS build of Vite's Node API is deprecated" warning
        # that our own validator misreads as a build failure. Context-aware:
        # mirror the logic in engine.py _protect_package_json.
        if pkg.get("type") == "module":
            all_deps = {**(pkg.get("dependencies") or {}),
                        **(pkg.get("devDependencies") or {})}
            uses_esm = any(
                r in all_deps for r in ("vite", "vitest", "@vitejs/plugin-react",
                                         "@vitejs/plugin-vue", "rollup")
            )
            if not uses_esm:
                pkg.pop("type", None)
        self._config_bypass = True
        try:
            with open(pkg_path, "w") as f:
                _json.dump(pkg, f, indent=2)
                f.write("\n")
        finally:
            self._config_bypass = False
        # Update manifest so the codemap reflects the change.
        with open(pkg_path) as f:
            self.manifest.record("package.json", f.read())
        result = {"status": "ok", "name": name, "version": effective_version, "kind": key}
        if was_coerced:
            result["coerced_from"] = version
            result["note"] = (
                f"Version {version!r} coerced to {effective_version!r} for host compatibility."
            )
        return result

    # ── Dispatch ──

    def dispatch(self, fn_name: str, fn_args: dict) -> dict:
        """Execute a tool by name."""
        method = getattr(self, fn_name, None)
        if method is None:
            return {"error": f"Unknown tool: {fn_name}"}
        if not isinstance(fn_args, dict):
            return {"error": "Arguments must be a JSON object"}
        try:
            return method(**fn_args)
        except Exception as e:
            return {"error": f"Tool error ({type(e).__name__}): {e}"}

    # ── Helpers ──

    def _scope_check(self, path: str) -> str | None:
        """Override point for path scoping. Returns error or None."""
        return None

    @staticmethod
    def _find_nearby(content: str, old_text: str, context_lines: int = 3) -> str | None:
        lines = content.split("\n")
        old_lines = old_text.split("\n")
        if not old_lines:
            return None

        best_ratio = 0
        best_start = 0
        window = len(old_lines)

        for i in range(len(lines)):
            end = min(i + window, len(lines))
            candidate = "\n".join(lines[i:end])
            ratio = SequenceMatcher(None, old_text, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i

        if best_ratio < 0.3:
            return None

        start = max(0, best_start - context_lines)
        end = min(len(lines), best_start + len(old_lines) + context_lines)
        return "\n".join(f"{i+1}: {lines[i]}" for i in range(start, end))


class ModuleScopedExecutor(ToolExecutor):
    """ToolExecutor scoped to a module's directory.

    WRITE operations (write_file, edit_file, delete_file) are restricted to module_path.
    READ operations (read_file, list_files, search_files) can access the FULL workspace.
    run_command is NOT restricted (needs full workspace for imports/pytest).
    """

    def __init__(self, workspace: str, manifest, module_path: str, progress_fn=None,
                 context_budget: int | None = None):
        super().__init__(workspace, manifest, progress_fn=progress_fn,
                         context_budget=context_budget)
        self.module_path = module_path.rstrip("/")

    def normalize_path(self, path: str) -> str:
        """Normalize path for WRITE operations — auto-prefixes module path."""
        path = super().normalize_path(path)
        # Auto-prefix module_path if not already present
        if not path.startswith(self.module_path + "/") and not path.startswith(self.module_path + os.sep):
            path = f"{self.module_path}/{path}"
        return path

    def _normalize_read_path(self, path: str) -> str:
        """Normalize path for READ operations — allows full workspace access.

        If the path exists at workspace root, use it as-is.
        Otherwise, try prefixing module_path (LLM might mean a file within the module).
        """
        path = super().normalize_path(path)
        # Check if path exists at workspace root first
        if os.path.exists(os.path.join(self.workspace, path)):
            return path
        # Try module-prefixed path
        module_path = f"{self.module_path}/{path}"
        if os.path.exists(os.path.join(self.workspace, module_path)):
            return module_path
        # Return as-is (will produce a clean error)
        return path

    def read_file(self, path: str, line_start: int = None, line_end: int = None,
                  offset: int = None, limit: int = None,
                  start_line: int = None, end_line: int = None) -> dict:
        """Read file with full workspace access (not scoped to module)."""
        path = _coerce(path, str)
        if not path:
            return {"error": "path is required"}
        path = self._normalize_read_path(path)
        # Accept both start_line/end_line (primary, matches line_edit) and
        # line_start/line_end (legacy) — larger Qwens and our line_edit use
        # the former; some smaller models use the latter.
        if start_line is not None:
            line_start = _coerce_int(start_line)
        else:
            line_start = _coerce_int(line_start)
        if end_line is not None:
            line_end = _coerce_int(end_line)
        else:
            line_end = _coerce_int(line_end)
        # Also accept offset/limit aliases
        if offset is not None and line_start is None:
            line_start = _coerce_int(offset) or 1
        if limit is not None and line_end is None and line_start is not None:
            limit_int = _coerce(limit, int, 100)
            limit_int = min(max(1, limit_int), self._read_lines)
            line_end = line_start + limit_int - 1
        if err := self._check_path(path):
            return {"error": err}
        if path in self.manifest.files and not line_start and not line_end and not self.build_mode:
            info = self.manifest.files[path]
            return {
                "path": path,
                "note": "You wrote this file. Use edit_file to fix issues.",
                "lines": info["lines"], "bytes": info["bytes"], "version": info["version"],
                "structure": info["summary"],
            }
        full = self._full_path(path)
        try:
            with open(full) as f:
                if line_start or line_end:
                    lines = f.readlines()
                    start = max(0, (line_start or 1) - 1)
                    end = min(len(lines), line_end or len(lines))
                    content = "".join(lines[start:end])
                    return {"path": path, "lines": f"{start+1}-{end}", "total_lines": len(lines),
                            "content": content[:self._read_chars]}
                else:
                    content = f.read(self._read_chars)
                    total = content.count("\n") + 1
                    result = {"path": path, "total_lines": total, "content": content}
                    if os.path.getsize(full) > self._read_chars:
                        result["truncated"] = True
                    return result
        except Exception as e:
            return {"error": str(e)}

    def list_files(self, path: str) -> dict:
        """List files with full workspace access (not scoped to module)."""
        path = _coerce(path, str, ".")
        path = self._normalize_read_path(path)
        if err := self._check_path(path):
            return {"error": err}
        full = self._full_path(path)
        try:
            result = subprocess.run(
                ["find", ".", "-type", "f", "-not", "-path", "./.git/*",
                 "-not", "-path", "./__pycache__/*", "-not", "-path", "./node_modules/*",
                 "-not", "-path", "./.venv/*"],
                cwd=full, capture_output=True, text=True, timeout=5,
            )
            files = sorted(result.stdout.strip().split("\n")) if result.stdout.strip() else []
            return {"path": path, "files": files, "count": len(files)}
        except Exception as e:
            return {"error": str(e)}

    def _scope_check(self, path: str) -> str | None:
        """Reject WRITE paths outside the module directory."""
        if not path.startswith(self.module_path + "/") and not path.startswith(self.module_path + os.sep):
            return f"BLOCKED: Path '{path}' is outside module scope '{self.module_path}/'. Write only files in your module."
        return None
