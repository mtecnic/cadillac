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


_VENV_DIRNAME = ".venv"


def workspace_venv_python(workspace: str) -> str | None:
    """Path to the workspace venv's python, creating the venv on first use.

    Builds install their own dependencies, and until now `pip3 install` ran
    against the HOST interpreter. A generated requirements.txt pinning
    `pydantic==2.6.1` / `pydantic-settings==2.1.0` was installed globally and
    downgraded the machine's own packages from 2.12.5 / 2.13.1, which broke the
    unrelated `mcp` SDK for everything on the box. An unattended builder must
    not be able to do that.

    `--system-site-packages` is deliberate: everything already installed on the
    host stays importable, so builds keep working exactly as before, but
    anything a build INSTALLS lands in the workspace and merely shadows the
    host copy. Nothing outside the workspace is ever modified, and deleting the
    workspace fully undoes it.

    Returns None when the venv cannot be created; callers must then refuse the
    install rather than silently falling back to the host interpreter.
    """
    venv_dir = os.path.join(workspace, _VENV_DIRNAME)
    # `python3`, not `python`: the command allowlist permits python3, and every
    # venv creates both symlinks. Using the bare name would need an allowlist
    # change purely as a side effect of this rewrite.
    py = os.path.join(venv_dir, "bin", "python3")
    pip = os.path.join(venv_dir, "bin", "pip")
    if os.path.exists(py) and os.path.exists(pip):
        return py

    # Two creators, because stdlib venv is not always usable: on Debian/Ubuntu
    # without the `python3-venv` package, `python3 -m venv` happily creates the
    # interpreter symlinks and then fails at ensurepip, leaving a venv with NO
    # pip in it. That half-built state is why this verifies pip exists rather
    # than trusting the return code. `virtualenv` bundles its own pip and needs
    # no system package, so it is the fallback.
    attempts = (
        ["python3", "-m", "venv", "--system-site-packages", venv_dir],
        ["python3", "-m", "virtualenv", "--system-site-packages", venv_dir],
    )
    for cmd in attempts:
        try:
            os.makedirs(workspace, exist_ok=True)
            subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception:
            continue
        if os.path.exists(py) and os.path.exists(pip):
            return py
    return None


def _venv_env(workspace: str) -> dict:
    """Environment for workspace commands, with the venv's bin/ first on PATH.

    Makes `python3`, `pip3` and console scripts like `pytest` resolve to the
    workspace venv when one exists, so a build's own installs are what its
    tests actually run against.
    """
    env = dict(os.environ)
    venv_dir = os.path.join(workspace, _VENV_DIRNAME)
    bin_dir = os.path.join(venv_dir, "bin")
    if os.path.isdir(bin_dir):
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = venv_dir
        # A stale PYTHONHOME would defeat the venv entirely.
        env.pop("PYTHONHOME", None)
    return env


def _redirect_pip_to_venv(command: str, venv_python: str) -> str:
    """Point every pip-install invocation in `command` at the workspace venv.

    Rewrites the pip executable only — flags, packages and `-r requirements.txt`
    are left exactly as written, so the build's intent is preserved and only its
    DESTINATION changes. `--break-system-packages` is stripped: it exists solely
    to override the host's PEP 668 guard and is meaningless (and alarming)
    inside a venv.
    """
    out = re.sub(
        r'(?<![\w./-])(?:python3?\s+-m\s+pip|pip3?)(?=\s+install\b)',
        f'{venv_python} -m pip',
        command,
    )
    return out.replace(" --break-system-packages", "")


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
    # Compiled-language toolchains added during Phase 2 — without these,
    # the LLM has to wrap every invocation in `cd && go test` which is
    # awkward and breaks plain `go version` style probes.
    "go", "gofmt", "goimports",
    "cargo", "rustc", "rustfmt",
    # PHP / WordPress family
    "php", "phpunit", "composer",
    "ls", "cat", "head", "tail", "wc", "find", "grep", "sort", "uniq",
    "mkdir", "rm", "cp", "mv", "touch", "chmod", "diff",
    "echo", "env", "which", "file", "stat", "curl",
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


# ── Shape-based command policy ───────────────────────────────────────────────
#
# The allowlist above answers "is this program permitted?". It cannot answer
# "what is this invocation going to touch?", and that is where the real holes
# were: `rm -rf ..` and `echo x > ~/.bashrc` both use allowlisted programs and
# both escape the workspace. `os.path.isabs("~/.bashrc")` is False (the shell
# expands the tilde, not Python), so the old redirect check missed it entirely.
#
# These rules classify each command segment by what it will *touch* — resolving
# every path-like token against the workspace — and deny by shape. Cadillac runs
# unattended with no human approval gate, so this layer has to be the backstop
# that an interactive agent gets from the user pressing "y".
#
# Design constraint: false positives break builds. Every rule below only fires
# on tokens that genuinely look like filesystem paths, and legitimate build
# traffic (`curl http://localhost:8000/health` in WIRING, `rm -rf node_modules`,
# `cd frontend && npm install`, an in-workspace `.env`) must stay allowed. The
# test suite pins both sides of each rule.

# Wrappers that don't change what the underlying program does; skipped so
# `timeout 60 rm -rf ..` is judged as `rm`.
_WRAPPER_COMMANDS = frozenset({"timeout", "env", "command", "exec", "nice", "nohup", "true"})

# Programs that write/destroy filesystem state. A path argument outside the
# workspace is a hard denial for these.
_WRITE_COMMANDS = frozenset({
    "rm", "mv", "cp", "mkdir", "touch", "chmod", "tee", "truncate", "ln", "dd", "shred",
})

# Programs that read file contents or enumerate the filesystem. Used for the
# credential-read and read-escape rules. `ls`/`find` are included: `ls ~/.ssh/`
# and `find / -name id_rsa` are reconnaissance, and both were allowed before.
_READ_COMMANDS = frozenset({
    "cat", "head", "tail", "less", "more", "od", "xxd", "strings", "base64",
    "cp", "mv", "grep", "sort", "uniq", "wc", "diff", "file", "stat",
    "ls", "find", "tar", "zip",
})

# Network-capable programs on the allowlist. Used for the exfiltration rule.
_NET_COMMANDS = frozenset({"curl", "wget", "nc", "ncat", "scp", "rsync", "ftp"})

# Host credential locations. These never legitimately appear in a Cadillac
# build, so they are denied wherever they resolve. Deliberately EXCLUDES bare
# `.env`: generated projects create their own `.env` inside the workspace and
# that is normal — an out-of-workspace `.env` is caught by the escape rule.
_HOST_CRED_RE = re.compile(
    r"(?:^|/)\.ssh(?:/|$)"
    r"|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)\b"
    r"|(?:^|/)\.aws/credentials"
    r"|(?:^|/)\.(?:netrc|npmrc|pgpass|htpasswd|git-credentials)\b"
    r"|(?:^|/)\.docker/config\.json"
    r"|(?:^|/)\.kube/config"
    r"|/etc/(?:shadow|sudoers)\b",
    re.IGNORECASE,
)

# Credential-ish names that are only suspicious OUTSIDE the workspace.
_OUTSIDE_CRED_RE = re.compile(
    r"(?:^|/)[^/]*\.env(?:\.|$)"
    r"|(?:^|/)[^/]*\.(?:pem|key)$"
    r"|(?:^|/)credentials?$",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)

# Device paths that are ordinary shell plumbing, not filesystem escapes.
# `cat /dev/null > tsconfig.json` and `... 2>/dev/null` are everywhere in real
# build traffic. Any OTHER /dev/ path still falls through to the escape rule
# (it resolves outside the workspace), and redirecting INTO a device is judged
# separately by the device_overwrite rule, so this stays safe.
_BENIGN_DEVICES = frozenset({
    "/dev/null", "/dev/zero", "/dev/urandom", "/dev/random",
    "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty",
})


def _expand_path_token(token: str) -> str:
    """Expand ~ and $HOME/${HOME} in a token the way the shell would.

    `os.path.isabs("~/.bashrc")` is False, which is exactly how the old
    redirect guard was bypassed. Expanding first makes the escape visible.
    """
    token = token.strip().strip("'\"")
    if token.startswith("@"):  # curl -d @file
        token = token[1:]
    token = token.replace("${HOME}", os.path.expanduser("~")).replace("$HOME", os.path.expanduser("~"))
    return os.path.expanduser(token)


def _looks_like_path(token: str) -> bool:
    """True when a token is plausibly a filesystem path.

    Conservative on purpose: anything with whitespace (a `python3 -c` payload),
    a URL scheme, or a leading dash is not treated as a path, so inline code and
    flags never trip the escape rules.
    """
    if not token or token.startswith("-"):
        return False
    if _URL_RE.match(token):
        return False
    if any(c.isspace() for c in token):
        return False
    return "/" in token or token in (".", "..") or token.startswith((".", "~", "$HOME", "${HOME}"))


def _escapes_workspace(token: str, workspace: str) -> bool:
    """True when a path-like token resolves outside the workspace."""
    expanded = _expand_path_token(token)
    if not expanded:
        return False
    base = os.path.realpath(workspace)
    full = os.path.realpath(expanded if os.path.isabs(expanded) else os.path.join(workspace, expanded))
    return not (full == base or full.startswith(base + os.sep))


def _extract_subshells(command: str) -> list[str]:
    """Return the bodies of $(...), `...`, and (...) so nested commands are judged.

    The old `_split_command_parts` respects quotes but never descends, so
    `echo $(rm -rf ..)` presented as a single allowlisted `echo` invocation.

    Quote-aware, matching real shell semantics: nothing inside single quotes is
    a substitution; inside double quotes `$(` and backticks still expand but a
    bare `(` is literal text. Without this, a `python3 -c "f(0, '..')"` payload
    reads as a subshell and trips the allowlist on its arguments.
    """
    bodies: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    while i < n:
        c = command[i]
        if c == "\\" and not in_single and i + 1 < n:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if in_single:
            i += 1
            continue
        if c == "`":
            close = command.find("`", i + 1)
            if close == -1:
                break
            body = command[i + 1:close]
            bodies.append(body)
            bodies.extend(_extract_subshells(body))
            i = close + 1
            continue
        # A bare `(` only opens a subshell outside double quotes; `$(` always does.
        is_dollar_paren = c == "$" and i + 1 < n and command[i + 1] == "("
        if is_dollar_paren or (c == "(" and not in_double):
            start = i + 2 if is_dollar_paren else i + 1
            depth = 1
            j = start
            while j < n and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                body = command[start:j - 1]
                bodies.append(body)
                bodies.extend(_extract_subshells(body))
                i = j
                continue
        i += 1
    return bodies


def _segment_tokens(part: str) -> list[str]:
    """Tokenize one command segment, quote-aware, tolerant of shell syntax."""
    import shlex
    try:
        return shlex.split(part, comments=False, posix=True)
    except ValueError:
        return part.split()


def _redirect_targets(command: str) -> list[str]:
    """Every redirect target in a command, including >> and 2>."""
    out = []
    for m in re.finditer(r"(?<![0-9<>])[0-9]?>>?\s*([^\s;|&()]+)", command):
        target = m.group(1)
        if target.startswith("&"):  # 2>&1
            continue
        out.append(target)
    return out


def _check_command_shape(command: str, workspace: str | None) -> tuple[bool, str]:
    """Classify one command by what it touches and deny dangerous shapes.

    Returns (allowed, reason). Called for the top-level command and, via
    `_extract_subshells`, for every nested command substitution.
    """
    # Redirect targets: expanded before the escape test, so `> ~/.bashrc` and
    # `> /etc/hosts` are both caught. Runs even without a workspace for the
    # absolute-path case.
    for target in _redirect_targets(command):
        expanded = _expand_path_token(target)
        if expanded.startswith("/dev/"):
            if expanded not in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                return False, f"Blocked: redirect to device '{target}' (device_overwrite)"
            continue
        if workspace is not None:
            if _escapes_workspace(target, workspace):
                return False, f"Blocked: redirect outside workspace '{target}' (path_escape)"
        elif os.path.isabs(expanded):
            return False, f"Blocked: redirect to absolute path '{target}'"

    for part in _split_command_parts(command):
        part = re.sub(r"\d*>>&?\s*\S+", "", part)
        part = re.sub(r"\d*>&?\d+", "", part)
        tokens = _segment_tokens(part.strip())
        while tokens and os.path.basename(tokens[0]) in _WRAPPER_COMMANDS:
            tokens = tokens[1:]
            # `timeout 60 cmd` / `env FOO=1 cmd`: drop the wrapper's own operands
            while tokens and (re.fullmatch(r"\d+[smhd]?", tokens[0]) or "=" in tokens[0].split("/")[0]):
                tokens = tokens[1:]
        if not tokens:
            continue
        exe = os.path.basename(tokens[0])
        args = [t for t in tokens[1:] if not t.startswith("-")]

        # Host credential access — denied wherever it resolves.
        for arg in args:
            if _HOST_CRED_RE.search(_expand_path_token(arg)):
                if exe in _READ_COMMANDS or exe in _WRITE_COMMANDS or exe in _NET_COMMANDS:
                    return False, f"Blocked: host credential path '{arg}' (credential_access)"

        # Credential exfiltration: a network program carrying a credential path.
        if exe in _NET_COMMANDS:
            for arg in args:
                expanded = _expand_path_token(arg)
                if _HOST_CRED_RE.search(expanded) or (
                    workspace is not None
                    and _looks_like_path(arg)
                    and _escapes_workspace(arg, workspace)
                    and _OUTSIDE_CRED_RE.search(expanded)
                ):
                    return False, f"Blocked: network command reading '{arg}' (credential_exfiltration)"

        if workspace is None:
            continue

        # Path escape on write and read programs. `rm -rf ..` lands here.
        if exe in _WRITE_COMMANDS or exe in _READ_COMMANDS:
            for arg in args:
                if not _looks_like_path(arg):
                    continue
                if _expand_path_token(arg).rstrip("/") in _BENIGN_DEVICES:
                    continue
                if _escapes_workspace(arg, workspace):
                    expanded = _expand_path_token(arg)
                    if _OUTSIDE_CRED_RE.search(expanded) or _HOST_CRED_RE.search(expanded):
                        return False, f"Blocked: credential path outside workspace '{arg}' (credential_access)"
                    verb = "writes" if exe in _WRITE_COMMANDS else "reads"
                    return False, f"Blocked: {exe} {verb} outside workspace '{arg}' (path_escape)"

    return True, ""


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


def validate_command(command: str, workspace: str | None = None) -> tuple[bool, str]:
    """Check if a command is allowed.

    Three layers, in order:
      1. BLOCKED_PATTERNS — known-dangerous literal shapes.
      2. Allowlist — is this program permitted at all? (applied to nested
         command substitutions too, not just the top level).
      3. Shape policy — what will this invocation actually touch? Path escapes,
         credential access, and exfiltration, resolved against `workspace`.

    `workspace` is optional for backward compatibility; when omitted, layer 3
    still runs the workspace-independent rules (host credentials, devices,
    absolute-path redirects).

    Fail-closed: an unexpected error inside the shape analysis denies the
    command rather than letting it through. Cadillac has no human approval
    gate, so an analyzer bug must not become an open door.
    """
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            return False, f"Blocked: matches dangerous pattern"

    # Allowlist over the top-level command AND every nested substitution, so
    # `echo $(git push)` cannot smuggle a non-allowlisted program through.
    for scope in [command, *_extract_subshells(command)]:
        for part in _split_command_parts(scope):
            part = re.sub(r'\d*>>&?\s*\S+', '', part)  # 2>/dev/null, >>/file
            part = re.sub(r'\d*>&?\d+', '', part)       # 2>&1
            part = part.strip()
            tokens = part.split()
            if not tokens:
                continue
            # Strip subshell/group punctuation so `(rm -rf ..)` reports as `rm`
            # rather than the meaningless token `(rm`.
            cmd = os.path.basename(tokens[0].lstrip("({").rstrip(")}"))
            if not cmd:
                continue
            if cmd not in ALLOWED_COMMANDS:
                return False, f"Command '{cmd}' not in allowlist. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"

    try:
        for scope in [command, *_extract_subshells(command)]:
            allowed, reason = _check_command_shape(scope, workspace)
            if not allowed:
                return False, reason
    except Exception as e:  # pragma: no cover - defensive
        return False, f"Blocked: command policy could not evaluate this command ({type(e).__name__})"

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


# ── Argument validation at the tool boundary ─────────────────────────────────
#
# The declared schema (TOOL_DEFS) is what the model is told; the method
# signature is what actually runs. Validating against BOTH means:
#   * a key the schema omits but the implementation accepts (read_file's
#     `offset`/`limit`/`start_line` aliases) keeps working, and
#   * a key neither accepts is reported by name with the real alternatives,
#     instead of leaking a Python TypeError the model has to reverse-engineer.

TOOL_SCHEMAS: dict[str, dict] = {
    d["function"]["name"]: d["function"].get("parameters", {}) or {}
    for d in TOOL_DEFS
}

_JSON_TYPE_COERCERS = {
    # Scalars stringify (models often send 42 for a string field); containers
    # do not — `str(['a','b'])` would silently become the literal "['a', 'b']".
    "string": lambda v: v if isinstance(v, str) else (
        str(v) if isinstance(v, (int, float, bool)) else None
    ),
    "integer": _coerce_int,
    "number": lambda v: v if isinstance(v, (int, float)) else _coerce_int(v),
    "boolean": lambda v: v if isinstance(v, bool) else (
        True if str(v).strip().lower() in ("true", "1", "yes")
        else False if str(v).strip().lower() in ("false", "0", "no", "none", "")
        else None
    ),
    "array": lambda v: v if isinstance(v, list) else None,
    "object": lambda v: v if isinstance(v, dict) else None,
}


def _accepted_parameters(method) -> set[str] | None:
    """Parameter names the bound method actually accepts.

    Returns None when the method takes **kwargs (accept anything). Used so the
    validator never rejects an argument the implementation supports just
    because the published schema doesn't mention it.
    """
    import inspect
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return None
    names = set()
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if name == "self":
            continue
        names.add(name)
    return names


def validate_tool_args(fn_name: str, fn_args: dict, method=None) -> tuple[dict, str | None, list[str]]:
    """Validate and coerce tool arguments against the declared schema.

    Returns ``(cleaned_args, error, notes)``:
      * ``error`` is a single actionable sentence when the call cannot proceed
        (missing required parameter, or a required value of the wrong shape).
      * ``notes`` records non-fatal corrections (dropped unknown keys, renamed
        near-misses) so the model can self-correct without a failed round.

    Fail-safe rather than fail-closed by design: this gate exists to save
    round-trips, not to enforce security. The real boundaries (path
    confinement, command policy, config guard) live inside the tool methods
    and run after this. When intent is unambiguous the call proceeds.
    """
    from difflib import get_close_matches

    schema = TOOL_SCHEMAS.get(fn_name, {})
    properties: dict = schema.get("properties", {}) or {}
    required: list = schema.get("required", []) or []
    declared = set(properties)
    accepted = _accepted_parameters(method) if method is not None else None
    # Everything the call may legally carry: declared schema keys plus any
    # extra keyword the implementation accepts.
    allowed = declared if accepted is None else (declared | accepted)

    cleaned: dict = {}
    notes: list[str] = []

    for key, value in fn_args.items():
        if not isinstance(key, str):
            notes.append(f"dropped non-string argument key {key!r}")
            continue
        if accepted is None or key in allowed:
            cleaned[key] = value
            continue
        # Unknown key: try to rescue an obvious typo before dropping it.
        match = get_close_matches(key, sorted(allowed), n=1, cutoff=0.75)
        if match and match[0] not in fn_args:
            cleaned[match[0]] = value
            notes.append(f"renamed unknown argument '{key}' to '{match[0]}'")
        else:
            notes.append(f"dropped unknown argument '{key}'")

    # Coerce declared types. A coercion that fails on an OPTIONAL parameter
    # drops it with a note; on a REQUIRED one it is a hard error.
    for key in list(cleaned):
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        coercer = _JSON_TYPE_COERCERS.get(spec.get("type"))
        if coercer is None:
            continue
        coerced = coercer(cleaned[key])
        if coerced is None and cleaned[key] is not None:
            if key in required:
                return cleaned, (
                    f"{fn_name}: parameter '{key}' must be a {spec.get('type')}, "
                    f"got {type(cleaned[key]).__name__}"
                ), notes
            del cleaned[key]
            notes.append(f"dropped '{key}' (expected {spec.get('type')})")
            continue
        cleaned[key] = coerced

    missing = [k for k in required if k not in cleaned or cleaned[k] is None]
    if missing:
        signature = ", ".join(
            f"{name} ({(properties.get(name) or {}).get('type', 'any')})"
            for name in properties
        ) or "none"
        return cleaned, (
            f"{fn_name}: missing required parameter(s) "
            f"{', '.join(repr(m) for m in missing)}. "
            f"Accepted parameters: {signature}."
        ), notes

    return cleaned, None, notes


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
        # Dependency installs go into a workspace-local venv, never the host.
        # A build previously installed a generated requirements.txt globally and
        # downgraded the machine's pydantic (2.12.5 -> 2.6.1), breaking the
        # unrelated `mcp` SDK for everything on the box. Fail CLOSED: if the
        # venv cannot be created we refuse the install, because the fallback is
        # exactly the damage being prevented.
        if _PIP_INSTALL_RE.search(command):
            venv_py = workspace_venv_python(self.workspace)
            if not venv_py:
                return {
                    "exit_code": 1, "stdout": "",
                    "stderr": (
                        "Refusing to install: could not create the workspace "
                        "virtualenv, and installing into the host Python is not "
                        "permitted (it can downgrade system packages). Check that "
                        "`python3 -m venv` works, then retry."
                    ),
                }
            command = _redirect_pip_to_venv(command, venv_py)
        else:
            # PEP 668 only applies to host-Python installs; venv pip is exempt.
            command = _rewrite_pip_for_pep668(command)
        allowed, reason = validate_command(command, workspace=self.workspace)
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
                # venv bin/ first on PATH so `python3`, `pytest` and friends
                # resolve to what this build installed, not the host copies.
                env=_venv_env(self.workspace),
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
            # Multi-language projects (e.g., Python ML + TS frontend) hit this
            # when the primary language is python and the engine never ran the
            # node-family scaffolding for them. Bootstrap a minimal package.json
            # so the LLM can build out the JS side without being blocked.
            # The fields here mirror what `_setup_typescript_project` would
            # have written for a vanilla Node project; scripts stays empty so
            # the LLM is free to add whichever test runner / build tool it
            # picks.
            try:
                pkg = {
                    "name": "project",
                    "version": "1.0.0",
                    "private": True,
                    "dependencies": {},
                    "devDependencies": {},
                    "scripts": {},
                }
                self._config_bypass = True
                try:
                    with open(pkg_path, "w") as f:
                        _json.dump(pkg, f, indent=2)
                        f.write("\n")
                finally:
                    self._config_bypass = False
            except OSError as e:
                return {"error": f"could not bootstrap package.json: {e}"}
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
        """Execute a tool by name, validating arguments at the boundary.

        Previously this was `method(**fn_args)` inside a bare try, so a single
        hallucinated key surfaced as a Python traceback string:

            Tool error (TypeError): write_file() got an unexpected keyword
            argument 'filename'

        The model then had to guess the real parameter name from a message that
        never states it. Validating first turns that into one actionable line
        naming the accepted parameters — and, where the intent is unambiguous
        (a near-miss on a real parameter, or an extra key alongside a complete
        valid call), the call proceeds instead of costing a round-trip.
        """
        method = getattr(self, fn_name, None)
        if method is None or not callable(method) or fn_name.startswith("_"):
            known = ", ".join(sorted(TOOL_SCHEMAS))
            return {"error": f"Unknown tool: {fn_name}. Available tools: {known}"}
        if not isinstance(fn_args, dict):
            return {"error": f"{fn_name}: arguments must be a JSON object, got {type(fn_args).__name__}"}

        cleaned, error, notes = validate_tool_args(fn_name, fn_args, method)
        if error:
            return {"error": error}
        try:
            result = method(**cleaned)
        except Exception as e:
            return {"error": f"Tool error ({type(e).__name__}): {e}"}
        # Surface dropped/renamed keys alongside a successful result so the
        # model corrects itself on the next call without a failed round.
        if notes and isinstance(result, dict) and "error" not in result:
            result = {**result, "arg_warnings": notes}
        return result

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

    def run_command(self, command: str) -> dict:
        """Run a shell command, correcting the cwd misconception on denial.

        Write paths are module-relative (a bare `foo.py` is auto-prefixed with
        module_path), but the shell deliberately runs at the WORKSPACE ROOT so
        pytest and cross-module imports resolve. That mismatch reliably teaches
        the model it is "inside" the module dir, and it then reaches for `../`:
        observed across four real builds as `ls ../tests/`,
        `mv x ../tests/core/x.py`, `mkdir -p ../tests/core`. The policy denies
        those correctly — two of them would have written into the workspace
        parent — but a bare denial doesn't fix the belief that caused them, so
        the model tries a variation next round. Naming the actual cwd converts
        a repeated round-waster into a one-round correction.
        """
        result = super().run_command(command)
        if isinstance(result, dict) and "path_escape" in str(result.get("error", "")):
            result = {
                **result,
                "hint": (
                    f"Shell commands run from the WORKSPACE ROOT, not from "
                    f"'{self.module_path}/'. Use workspace-relative paths: this "
                    f"module's files are under '{self.module_path}/', and a sibling "
                    f"module is at its own top-level path. A leading '../' leaves "
                    f"the workspace and is always denied."
                ),
            }
        return result

    def normalize_path(self, path: str) -> str:
        """Normalize path for WRITE operations.

        Three cases:
          1. Path already starts with `module_path/` → use as-is.
          2. Path is a bare relative name (`foo.py`) → prefix with module_path.
          3. Path is a workspace-relative path to a file in a DIFFERENT
             module (e.g., scoped to `backend/api/` but LLM wrote
             `backend/db/repository.py`) → DON'T silently corrupt the path
             into `backend/api/backend/db/repository.py`. Return as-is so
             the downstream `_check_path` produces a clean
             "outside-of-module-scope" error the LLM can act on.
        """
        path = super().normalize_path(path)
        if path.startswith(self.module_path + "/") or path.startswith(self.module_path + os.sep):
            return path  # case 1
        # Case 3: looks like a workspace-relative path to a real file in a
        # different module. Heuristic: contains a "/" and the bare prefix
        # before the first "/" is a sibling top-level dir of `module_path`.
        if "/" in path:
            full_at_root = os.path.join(self.workspace, path)
            if os.path.exists(full_at_root):
                # Not in our module scope — caller's `_check_path` will
                # reject with a clean message.
                return path
        # Case 2: bare name, prefix with module_path.
        return f"{self.module_path}/{path}"

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
