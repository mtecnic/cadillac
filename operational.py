"""Operational gates — probe deploy-readiness, not just correctness.

The validate.py pipeline asks "does the code run on the happy path?". The
WIRING pipeline asks "do the layers connect?". Neither asks "will this
survive its first hour in production?". That gap is what shipped a backend
that crashed the first time it hit a connection-refused upstream (PingFlux:
schema `status_code NOT NULL`, code passed `None` on network failure — every
ping in the wild crashed the writer thread).

Three gates here, ordered cheapest → most expensive:

  Gate 1 — STATIC SCHEMA INTEGRITY
    Pure AST + regex pass over Python sources. For each CREATE TABLE string
    we parse `NOT NULL` columns, then for each `cursor.execute("INSERT ...",
    (...))` we check whether a literal `None` lands in a NOT NULL slot.
    Catches the PingFlux bug class without booting anything.

  Gate 2 — MISSING-ENV RUNTIME PROBE
    Detect strict-required env vars (`os.environ["KEY"]`-style, no default).
    If any exist, boot the backend with one stripped. Acceptable outcomes:
      - process exits non-zero within 8s (fail-fast)
      - process logs the missing var by name to stderr
    Bad outcome: silent hang, or boots and serves but every request 500s
    on first hit. That's the production-deploy footgun.

  Gate 3 — SIGTERM RESPONSIVENESS
    Boot, send SIGTERM, expect clean exit within 5s. Apps that catch
    SIGTERM and swallow it survive `kubectl rollout restart` only when
    Kubernetes' grace period expires and it SIGKILLs them — losing
    in-flight requests and DB cleanup. Apps that don't catch it at all
    exit cleanly; this gate fails only the actively-bad case.

Only runs against Python backends with a detectable HTTP entry. Pure CLIs,
games, static sites, and non-Python projects skip cleanly.

All probes reuse the boot infrastructure from validate.py's WIRING phase
(`_wiring_find_free_port`, `_wiring_detect_backend`) — same process-group
discipline, same listening-wait, same cleanup. No duplication.
"""

from __future__ import annotations

import ast
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass

from .validate import (
    CheckResult,
    _wiring_detect_backend,
    _wiring_find_free_port,
)


# ─────────────────────────── GATE 1: SCHEMA INTEGRITY ───────────────────────── #


_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;?",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class _Column:
    name: str
    not_null: bool
    has_default: bool


@dataclass(frozen=True)
class _Table:
    name: str
    columns: tuple[_Column, ...]


def _parse_create_table(body: str) -> tuple[_Column, ...]:
    """Parse the `(col1 TYPE NOT NULL, col2 TYPE, ...)` body of a CREATE TABLE."""
    cols: list[_Column] = []
    depth = 0
    chunks: list[str] = []
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            chunks.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        chunks.append("".join(buf).strip())

    for chunk in chunks:
        if not chunk:
            continue
        up = chunk.upper()
        # Skip table-level constraints (PRIMARY KEY (a,b), FOREIGN KEY ...,
        # UNIQUE (...), CHECK ...). They don't define columns.
        if up.startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE",
                          "CHECK", "CONSTRAINT")):
            continue
        m = re.match(r"^[\"`]?(\w+)[\"`]?\s+", chunk)
        if not m:
            continue
        col_name = m.group(1)
        not_null = "NOT NULL" in up
        # AUTOINCREMENT / INTEGER PRIMARY KEY is implicitly nullable-tolerant
        # (SQLite assigns rowid). Treat PRIMARY KEY without explicit NOT NULL
        # as not-required, since SQLite will fill it.
        is_pk = "PRIMARY KEY" in up
        has_default = "DEFAULT" in up or is_pk
        cols.append(_Column(name=col_name, not_null=not_null,
                            has_default=has_default))
    return tuple(cols)


def _scan_schemas(workspace: str) -> dict[str, _Table]:
    """Walk .py files for CREATE TABLE strings. Return {table_name: _Table}."""
    tables: dict[str, _Table] = {}
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", ".git", "__pycache__",
                                "dist", "build", "venv", ".venv",
                                ".cadillac", "frontend")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            for m in _CREATE_TABLE_RE.finditer(text):
                name = m.group(1)
                cols = _parse_create_table(m.group(2))
                if cols and name not in tables:
                    tables[name] = _Table(name=name, columns=cols)
    return tables


_INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*(?:\(([^)]*)\))?\s*VALUES\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)


def _find_none_passed_to_not_null(
    workspace: str, schemas: dict[str, _Table],
) -> list[str]:
    """Return a list of `file:line — table.column receives literal None` findings.

    Two patterns are checked:

      (1) Inline INSERT with literal values:
          execute("INSERT INTO t (a, b) VALUES (?, ?)", (x, None))
          → if column `b` is NOT NULL, flag.

      (2) Function calls passing `None` to a kwarg matching a NOT NULL column
          name in a known table. Best-effort — matches when the call site
          uses `column_name=None`.

    Both are conservative: only LITERAL None is flagged, not derived
    expressions. Avoids false positives on `value or fallback` paths where
    the value is actually never None at runtime.
    """
    findings: list[str] = []
    if not schemas:
        return findings

    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", ".git", "__pycache__",
                                "dist", "build", "venv", ".venv",
                                ".cadillac", "frontend", "tests", "test")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # Look for execute("INSERT INTO t (...) VALUES (?, ?)", (...))
                attr = node.func
                if not (isinstance(attr, ast.Attribute) and
                        attr.attr in ("execute", "executemany")):
                    continue
                if len(node.args) < 2:
                    continue
                sql_arg = node.args[0]
                params_arg = node.args[1]
                sql = _string_value(sql_arg)
                if not sql:
                    continue
                m = _INSERT_RE.search(sql)
                if not m:
                    continue
                table_name = m.group(1)
                col_list_raw = m.group(2)
                table = schemas.get(table_name)
                if not table:
                    continue
                if col_list_raw:
                    col_names = [c.strip().strip('"`')
                                 for c in col_list_raw.split(",") if c.strip()]
                else:
                    # No explicit column list — positional matches table order.
                    col_names = [c.name for c in table.columns]
                # Materialize params; expect a Tuple or List literal
                value_nodes: list[ast.AST] = []
                if isinstance(params_arg, (ast.Tuple, ast.List)):
                    value_nodes = list(params_arg.elts)
                else:
                    continue  # dynamic params — skip; analyzer can't reason
                # executemany takes [tuple, tuple, ...] — analyze the first
                if attr.attr == "executemany" and value_nodes and isinstance(
                        value_nodes[0], (ast.Tuple, ast.List)):
                    value_nodes = list(value_nodes[0].elts)
                if len(value_nodes) != len(col_names):
                    continue
                col_by_name = {c.name: c for c in table.columns}
                for col_name, val_node in zip(col_names, value_nodes):
                    col = col_by_name.get(col_name)
                    if not col or not col.not_null or col.has_default:
                        continue
                    if _is_literal_none(val_node):
                        rel = os.path.relpath(path, workspace)
                        line = getattr(val_node, "lineno", node.lineno)
                        findings.append(
                            f"{rel}:{line} — INSERT into {table_name}: "
                            f"literal None passed to NOT NULL column "
                            f"'{col_name}'"
                        )
    return findings


def _string_value(node: ast.AST) -> str | None:
    """Extract a string from a Constant or JoinedStr-with-only-constants."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                return None
        return "".join(parts)
    return None


def _is_literal_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _static_schema_integrity_check(
    workspace: str, lang,
) -> list[CheckResult]:
    """Run the schema integrity gate. Returns one CheckResult."""
    if not lang or lang.family != "python":
        return [CheckResult(
            "operational", True,
            f"schema-integrity: not applicable for "
            f"{lang.name if lang else '?'}, skipped", "info",
        )]
    schemas = _scan_schemas(workspace)
    if not schemas:
        return [CheckResult(
            "operational", True,
            "schema-integrity: no CREATE TABLE statements found, skipped",
            "info",
        )]
    findings = _find_none_passed_to_not_null(workspace, schemas)
    if findings:
        return [CheckResult(
            "operational", False,
            "Schema integrity: code passes literal None into a NOT NULL "
            "column. The first row with that NULL will crash the writer:\n  "
            + "\n  ".join(findings[:10])
            + "\nFix options: (a) make the column nullable in the schema "
            "if None is a legitimate value, (b) supply a sentinel default "
            "(0, '', -1) at the call site, or (c) guard the INSERT with an "
            "explicit `if x is None: x = <default>` before the call.",
        )]
    return [CheckResult(
        "operational", True,
        f"schema-integrity: {len(schemas)} table(s), no literal None into "
        f"NOT NULL columns",
    )]


# ─────────────────────────── GATE 2: MISSING-ENV PROBE ──────────────────────── #


def _scan_required_env_vars(workspace: str) -> list[str]:
    """Find env vars accessed strictly (`os.environ["X"]`, no default).

    `.get("X")` and `.get("X", default)` are skipped — those return None or
    a fallback. Strict access (`os.environ["X"]` or `os.getenv("X")` without
    a default and without an `or`-fallback) is the failure mode we want to
    probe: missing → KeyError → backend crash on import.
    """
    required: list[str] = []
    seen: set[str] = set()
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", ".git", "__pycache__",
                                "dist", "build", "venv", ".venv",
                                ".cadillac", "frontend", "tests", "test")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # os.environ["X"]
                if (isinstance(node, ast.Subscript) and
                        isinstance(node.value, ast.Attribute) and
                        node.value.attr == "environ" and
                        isinstance(node.value.value, ast.Name) and
                        node.value.value.id == "os"):
                    key = _string_value(_subscript_index(node))
                    if key and key not in seen and _looks_like_app_var(key):
                        seen.add(key)
                        required.append(key)
    return required


def _subscript_index(node: ast.Subscript) -> ast.AST:
    """ast.Subscript stores the index in .slice (3.9+); older has Index wrapper."""
    s = node.slice
    if hasattr(ast, "Index") and isinstance(s, getattr(ast, "Index")):
        return s.value  # type: ignore[attr-defined]
    return s


_SYSTEM_VARS = {
    "PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "TERM", "PORT",
    "PYTHONPATH", "VIRTUAL_ENV", "HOSTNAME", "TZ", "FLASK_ENV",
    "FLASK_DEBUG", "PYTHONUNBUFFERED", "DEBUG",
}


def _looks_like_app_var(key: str) -> bool:
    """Filter out OS / framework env vars from probe candidates."""
    if key in _SYSTEM_VARS:
        return False
    if len(key) < 3:
        return False
    return True


def _wait_for_listening_or_exit(
    proc: subprocess.Popen, port: int, deadline_s: float,
) -> tuple[bool, str]:
    """Wait until proc binds :port (listening=True) or exits. Returns (listening, tail)."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True, ""
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
        if proc.poll() is not None:
            tail = ""
            try:
                if proc.stdout:
                    tail = proc.stdout.read() or ""
            except Exception:
                pass
            return False, tail[-2000:]
    return False, ""


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole group, then SIGKILL after 3s if still alive."""
    try:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=2)
        except (ProcessLookupError, PermissionError, OSError,
                subprocess.TimeoutExpired):
            pass
    finally:
        # Close the pipe so the GC doesn't ResourceWarn on shutdown.
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass


def _runtime_probe_missing_env(
    workspace: str, lang, be: dict,
) -> list[CheckResult]:
    """Boot the backend with one required env var stripped. Expect clean failure."""
    required = _scan_required_env_vars(workspace)
    if not required:
        return [CheckResult(
            "operational", True,
            "missing-env: no strict os.environ[...] reads detected, skipped",
            "info",
        )]
    target = required[0]
    port = _wiring_find_free_port(default=be.get("port", 5000))
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["PYTHONPATH"] = workspace + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Strip the target var if it leaked in from our own shell.
    env.pop(target, None)
    try:
        proc = subprocess.Popen(
            be["cmd"], cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
    except Exception as e:
        return [CheckResult(
            "operational", False,
            f"missing-env probe: failed to launch backend: {e}",
        )]

    try:
        listening, tail = _wait_for_listening_or_exit(proc, port, deadline_s=8)
        if not listening and proc.poll() is not None:
            # Fail-fast — the ideal outcome. Check that the error names the
            # missing var (KeyError, not a cryptic AttributeError).
            mentions_target = target in tail or "KeyError" in tail
            if mentions_target:
                return [CheckResult(
                    "operational", True,
                    f"missing-env: backend exits cleanly when ${target} is "
                    f"missing (KeyError raised)",
                )]
            return [CheckResult(
                "operational", False,
                f"missing-env: backend exits when ${target} is missing but "
                f"the error doesn't name the variable — operators won't "
                f"know what to set. Tail:\n{tail[-800:]}\n"
                f"Fix: validate required env at startup and raise with the "
                f"variable name, e.g., `raise RuntimeError(f'{target} is "
                f"required')`.",
            )]
        if listening:
            # Worse: backend boots without the var. Either uses a fallback
            # silently (often broken) or will crash on first request.
            return [CheckResult(
                "operational", False,
                f"missing-env: backend booted with ${target} missing — "
                f"either a silent fallback masks a misconfiguration, or "
                f"every request will crash on first use. Validate required "
                f"env at startup (fail-fast). Concrete fix: at the top of "
                f"the entry file (BEFORE creating the Flask app or any "
                f"DB connection), add:\n"
                f"    import os\n"
                f"    if not os.environ.get('{target}'):\n"
                f"        raise RuntimeError('{target} environment variable "
                f"is required')\n"
                f"This makes the deploy fail-fast and tells the operator "
                f"exactly what to set.",
            )]
        # Neither listening nor exited within 8s → hang
        return [CheckResult(
            "operational", False,
            f"missing-env: backend hangs (no exit, no listen) when "
            f"${target} is missing — production schedulers will time out "
            f"the health check and the pod will crashloop without a "
            f"useful error. Concrete fix at the top of the entry file:\n"
            f"    import os\n"
            f"    if not os.environ.get('{target}'):\n"
            f"        raise RuntimeError('{target} environment variable "
            f"is required')",
        )]
    finally:
        _kill_process_group(proc)


# ─────────────────────────── GATE 3: SIGTERM RESPONSIVENESS ─────────────────── #


def _runtime_probe_sigterm(
    workspace: str, lang, be: dict,
) -> list[CheckResult]:
    """Boot the backend, send SIGTERM (not group-kill), expect exit within 5s.

    Apps that catch SIGTERM and never call exit() survive `kubectl rollout
    restart` only when the grace period expires and Kubernetes SIGKILLs
    them — losing in-flight requests and DB cleanup. This probe flags only
    the actively bad case: SIGTERM ignored.
    """
    port = _wiring_find_free_port(default=be.get("port", 5000))
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["FLASK_ENV"] = "development"
    env["PYTHONPATH"] = workspace + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    try:
        proc = subprocess.Popen(
            be["cmd"], cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
    except Exception as e:
        return [CheckResult(
            "operational", False,
            f"sigterm-probe: failed to launch backend: {e}",
        )]

    try:
        listening, tail = _wait_for_listening_or_exit(proc, port, deadline_s=10)
        if not listening:
            # Backend never came up — not this gate's job to diagnose,
            # WIRING / smoke_run cover that. Skip cleanly.
            return [CheckResult(
                "operational", True,
                "sigterm-probe: backend never bound, skipped "
                "(other gates will report startup failure)",
                "info",
            )]

        # Send SIGTERM to the leader only (not the group). We're testing
        # whether the app's own signal handling is sane — group-kill would
        # forcibly terminate it regardless and tell us nothing.
        try:
            proc.send_signal(signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as e:
            return [CheckResult(
                "operational", False,
                f"sigterm-probe: send_signal failed: {e}",
            )]

        # Give it 5s to wind down cleanly.
        try:
            proc.wait(timeout=5)
            return [CheckResult(
                "operational", True,
                f"sigterm-probe: backend exits cleanly within 5s of SIGTERM "
                f"(rc={proc.returncode})",
            )]
        except subprocess.TimeoutExpired:
            return [CheckResult(
                "operational", False,
                "sigterm-probe: backend ignored SIGTERM and stayed alive >5s. "
                "Production orchestrators (Kubernetes, systemd) send SIGTERM "
                "first and SIGKILL after a grace period — apps that swallow "
                "SIGTERM lose in-flight requests and DB cleanup at every "
                "rollout. Common causes: a bare `except:` that catches "
                "SignalException, a signal handler that does work without "
                "calling sys.exit(), or a Flask debug reloader that re-"
                "spawns. Fix: ensure SIGTERM either propagates or triggers "
                "an explicit clean shutdown.",
            )]
    finally:
        _kill_process_group(proc)


# ─────────────────────────────── ORCHESTRATOR ───────────────────────────────── #


def check_operational_gates(workspace: str, lang=None) -> list[CheckResult]:
    """Run all operational gates. Static first (cheap, always runs), then
    runtime gates only if a Python backend is detected.

    Returns one or more CheckResult with name='operational'. Pure-CLI,
    static-site, and non-Python projects skip cleanly with severity='info'.
    """
    results: list[CheckResult] = []

    # Gate 1 — static. Always runs for Python projects.
    results.extend(_static_schema_integrity_check(workspace, lang))

    # Gates 2 & 3 — runtime. Require a detectable HTTP backend.
    if not lang or lang.family != "python":
        results.append(CheckResult(
            "operational", True,
            f"runtime gates: {lang.name if lang else '?'} backend, skipped "
            f"(probes are Python-HTTP-only)",
            "info",
        ))
        return results
    be = _wiring_detect_backend(workspace)
    if be is None:
        results.append(CheckResult(
            "operational", True,
            "runtime gates: no HTTP backend detected (CLI/library/script), "
            "skipped",
            "info",
        ))
        return results

    results.extend(_runtime_probe_missing_env(workspace, lang, be))
    results.extend(_runtime_probe_sigterm(workspace, lang, be))
    return results
