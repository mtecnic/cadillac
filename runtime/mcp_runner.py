"""MCP (Model Context Protocol) runtime verification.

Boots the MCP server, speaks JSON-RPC 2.0 over stdio, and drives spec
stories through real MCP method calls (initialize → tools/list →
tools/call → resources/read → prompts/get, etc). Catches the class of
bug that neither the CLI probe (wrong shape — MCP is JSON-RPC not argv)
nor the HTTP probe (wrong transport — stdio not HTTP) can see.

The 2026-07-02 MCP file-indexer build was the trigger: 20 files shipped,
CLI-strategy RUNTIME fired 19 argv probes and 18 failed with "unknown
argument", producing zero actionable signal about whether the actual
MCP tools worked. This strategy dispatches BEFORE cli so the correct
protocol is used.

Per flow: send `initialize` first (protocol handshake), then walk the
step chain sending each as one JSON-RPC line to stdin and reading the
response from stdout. Assertions on result subset or error code.
Clean teardown via killpg.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field

from .types import Probe, ProbeFailure, VerificationResult


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MCPStep:
    """One JSON-RPC 2.0 request or notification against the MCP server.

    `params` may reference captured values via `{name}` substitution in
    string values. `expect_result` is a subset assertion against the
    successful response's `result` object; `expect_error_code` asserts
    the JSON-RPC error code (mutually exclusive with `expect_result`).
    `capture` extracts values with `$.result.path.to.field` syntax.
    """
    method: str                          # "initialize" | "tools/list" | "tools/call" | ...
    params: dict = field(default_factory=dict)
    is_notification: bool = False        # True for notifications like "notifications/initialized"
    expect_result: dict = field(default_factory=dict)
    expect_error_code: int | None = None  # e.g. -32601 (method not found)
    capture: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 5.0


@dataclass(frozen=True)
class MCPFlow(Probe):
    """A chained sequence of JSON-RPC calls that proves one story."""
    steps: tuple[MCPStep, ...] = ()


# ── LLM-driven generation ────────────────────────────────────────────────────


_GENERATE_SYSTEM_PROMPT = """You are a senior QA engineer writing executable \
MCP (Model Context Protocol) JSON-RPC flows for an autobuilder. Given a spec \
of user stories and the server's declared tools/resources, output flows that \
prove each story actually works against the running MCP server over stdio.

Output JSON only — no prose, no fences. Schema:

{
  "flows": [
    {
      "story_id": "S07",
      "title": "user searches indexed files",
      "priority": "must",
      "steps": [
        {
          "method": "tools/list",
          "params": {},
          "expect_result": {"tools": "exists"},
          "capture": {"tool_names": "$.result.tools[].name"}
        },
        {
          "method": "tools/call",
          "params": {"name": "search", "arguments": {"query": "hello", "limit": 5}},
          "expect_result": {"content": "exists", "isError": false}
        }
      ]
    }
  ]
}

Rules:
- The harness ALWAYS sends `initialize` and `notifications/initialized` for \
you first. Your flow starts AFTER the handshake completes. Do NOT include \
those steps yourself.
- One flow per must- and should-priority story. Skip "could".
- `method` uses the MCP standard names: tools/list, tools/call, \
resources/list, resources/read, prompts/list, prompts/get.
- For `tools/call`, `params` MUST be `{"name": "<tool-name>", "arguments": {...}}` \
where <tool-name> is one of the tools declared in the workspace summary below.
- `expect_result` supports subset match: `{"key": "exists"}`, \
`{"key": ">=1"}`, `{"key": "!=null"}`, or exact-match values. Assert on \
side-effect fields where possible: not just "response arrived" but "the \
searched value appeared in results".
- `expect_error_code` asserts a JSON-RPC error code — use for negative \
tests. Common: -32601 (method not found), -32602 (invalid params). Note: \
FastMCP tends to collapse these to -32602. For tool-call failures (unknown \
tool name, missing arg), FastMCP returns a SUCCESS response with \
`isError: true` inside the result — assert `expect_result={"isError": false}` \
on tool-call happy paths to catch broken tools.
- `capture` extracts values with `$.result.<path>`. Use `[].name` to \
extract the `.name` field from an array of objects.
- Use realistic inputs. If a tool takes a `query: str`, don't send an empty \
string — send a term likely to appear in a code file (e.g. "def", "class").
- 4-12 flows for typical MCP servers. Cover happy path AND negative path \
(bad tool name → error, missing required arg → error).

Output ONLY the JSON object."""


def _build_generate_prompt(spec, workspace: str, lang) -> str:
    stack_hint = f"Stack: Python + MCP SDK ({getattr(lang, 'name', 'python')})"
    spec_block = spec.to_prompt_block(max_chars=3500) if spec else ""
    tools_block = _summarize_mcp_surface(workspace)
    return (
        f"{stack_hint}\n\n"
        f"## DECLARED MCP TOOLS / RESOURCES\n{tools_block}\n\n"
        f"{spec_block}\n\n"
        "Generate JSON-RPC flows."
    )


_MCP_SURFACE_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "dist", "build",
    "venv", ".venv", ".cadillac", "frontend",
}


def _summarize_mcp_surface(workspace: str) -> str:
    """Extract declared tool/resource/prompt names from the workspace.

    Recognizes the two dominant MCP Python SDK patterns:

      1. FastMCP decorator style:
           @mcp.tool()
           async def search(query: str, limit: int = 10) -> list[dict]:
               ...

      2. Low-level Server registration:
           @server.list_tools()
           async def list_tools() -> list[Tool]:
               return [Tool(name="search", ...), Tool(name="get_file", ...)]

    Both surface as "search / get_file / ..." so the LLM's flow generator
    calls the right names.
    """
    tools: list[str] = []
    resources: list[str] = []
    prompts: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _MCP_SURFACE_SKIP_DIRS]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            # FastMCP decorator style (bare @mcp.tool())
            for m in re.finditer(
                r"@\w+\.tool\(\)\s*\n\s*(?:async\s+)?def\s+(\w+)\s*\(",
                text,
            ):
                tools.append(m.group(1))
            for m in re.finditer(
                r"@\w+\.resource\([^)]*\)\s*\n\s*(?:async\s+)?def\s+(\w+)",
                text,
            ):
                resources.append(m.group(1))
            for m in re.finditer(
                r"@\w+\.prompt\(\)\s*\n\s*(?:async\s+)?def\s+(\w+)",
                text,
            ):
                prompts.append(m.group(1))
            # Programmatic-name form (mcp_server.tool("search")(callable) — the
            # 2026-07-02 build used this shape from a ToolRegistry.register()).
            for m in re.finditer(
                r'\w+\.tool\s*\(\s*["\']([^"\']+)["\']\s*\)',
                text,
            ):
                tools.append(m.group(1))
            # Low-level Server: Tool(name="search", ...)
            for m in re.finditer(r'Tool\s*\(\s*name\s*=\s*["\']([^"\']+)["\']',
                                  text):
                tools.append(m.group(1))
    # Deduplicate preserving order
    def _uniq(xs):
        seen = set()
        out = []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out
    tools = _uniq(tools)
    resources = _uniq(resources)
    prompts = _uniq(prompts)
    if not (tools or resources or prompts):
        return "(no MCP tools / resources / prompts detected via static scan)"
    lines: list[str] = []
    if tools:
        lines.append("tools: " + ", ".join(tools))
    if resources:
        lines.append("resources: " + ", ".join(resources))
    if prompts:
        lines.append("prompts: " + ", ".join(prompts))
    return "\n".join(lines)


def generate_flows(spec, workspace: str, lang, cfg, emit) -> list[MCPFlow]:
    """Call the LLM to produce MCPFlow objects. Returns a list (may be empty)."""
    from cadillac.engine import chat, extract_json

    if spec is None or spec.is_empty():
        return []
    user_msg = _build_generate_prompt(spec, workspace, lang)
    messages = [
        {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    emit("log", msg="[RUNTIME/mcp] generating flows from spec + declared tools...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[RUNTIME/mcp] LLM error: {e}")
        return []
    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return []
    raw_flows = parsed.get("flows") or []
    if not isinstance(raw_flows, list):
        return []
    flows: list[MCPFlow] = []
    for entry in raw_flows:
        if not isinstance(entry, dict):
            continue
        f = _flow_from_dict(entry)
        if f is not None:
            flows.append(f)

    # Persist for inspection / iterate reuse
    try:
        cad_dir = os.path.join(workspace, ".cadillac")
        os.makedirs(cad_dir, exist_ok=True)
        with open(os.path.join(cad_dir, "mcp_flows.json"), "w") as fh:
            json.dump([_flow_to_dict(fl) for fl in flows], fh, indent=2)
    except OSError:
        pass

    emit("log", msg=f"[RUNTIME/mcp] {len(flows)} flow(s) generated")
    return flows


def _flow_from_dict(d: dict) -> MCPFlow | None:
    try:
        story_id = str(d.get("story_id", "")).strip()
        title = str(d.get("title", "")).strip()
        priority = str(d.get("priority", "must")).strip()
        if priority not in ("must", "should", "could"):
            priority = "must"
        raw_steps = d.get("steps") or []
        if not story_id or not isinstance(raw_steps, list):
            return None
        steps: list[MCPStep] = []
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            method = str(s.get("method", "")).strip()
            if not method:
                continue
            steps.append(MCPStep(
                method=method,
                params=s.get("params") if isinstance(s.get("params"), dict) else {},
                is_notification=bool(s.get("is_notification", False)),
                expect_result=s.get("expect_result") if isinstance(
                    s.get("expect_result"), dict) else {},
                expect_error_code=s.get("expect_error_code"),
                capture=s.get("capture") if isinstance(
                    s.get("capture"), dict) else {},
                timeout_s=float(s.get("timeout_s", 5.0) or 5.0),
            ))
        if not steps:
            return None
        return MCPFlow(story_id=story_id, title=title, priority=priority,
                       steps=tuple(steps))
    except (TypeError, ValueError):
        return None


def _flow_to_dict(f: MCPFlow) -> dict:
    return {
        "story_id": f.story_id,
        "title": f.title,
        "priority": f.priority,
        "steps": [
            {
                "method": s.method, "params": s.params,
                "is_notification": s.is_notification,
                "expect_result": s.expect_result,
                "expect_error_code": s.expect_error_code,
                "capture": s.capture,
                "timeout_s": s.timeout_s,
            } for s in f.steps
        ],
    }


# ── Server boot / teardown ────────────────────────────────────────────────────


def _boot_server(workspace: str, entry_cmd: list[str], emit
                 ) -> tuple[subprocess.Popen | None, str]:
    """Boot the MCP server subprocess with stdin/stdout as JSON-RPC channels.

    Returns (proc, err). On failure, proc is None and err is a diagnostic.
    Uses `start_new_session=True` so we can killpg the entire process group
    (MCP servers under FastMCP often spawn worker tasks or threads).
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"  # critical — line-buffered stdio
    env["PYTHONPATH"] = workspace + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.Popen(
            entry_cmd, cwd=workspace, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            bufsize=1,  # line-buffered
            start_new_session=True,
        )
    except Exception as e:
        return (None, f"failed to launch MCP server: {e}")
    # Give the server a moment to reach its stdio ready state
    time.sleep(0.3)
    if proc.poll() is not None:
        tail = ""
        try:
            tail = (proc.stderr.read() or "") if proc.stderr else ""
        except Exception:
            pass
        return (None, f"server exited at startup (rc={proc.returncode}): {tail[-1000:]}")
    return (proc, "")


def _kill_group(proc: subprocess.Popen) -> None:
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
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


# ── JSON-RPC send/recv ───────────────────────────────────────────────────────


def _rpc_request(method: str, params: dict, req_id: int,
                 is_notification: bool = False) -> str:
    """Encode a JSON-RPC 2.0 request as a single line."""
    payload: dict = {"jsonrpc": "2.0", "method": method, "params": params}
    if not is_notification:
        payload["id"] = req_id
    return json.dumps(payload) + "\n"


def _rpc_send(proc: subprocess.Popen, line: str) -> tuple[bool, str]:
    """Write one JSON-RPC line to the server's stdin."""
    if proc.stdin is None:
        return (False, "server stdin closed")
    try:
        proc.stdin.write(line)
        proc.stdin.flush()
        return (True, "")
    except (BrokenPipeError, OSError) as e:
        return (False, f"stdin write failed: {e}")


def _rpc_recv(proc: subprocess.Popen, timeout_s: float
              ) -> tuple[dict | None, str]:
    """Read one JSON-RPC line from stdout. Returns (parsed_json, err)."""
    if proc.stdout is None:
        return (None, "server stdout closed")
    import select
    deadline = time.time() + timeout_s
    line = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                if proc.stderr:
                    tail = proc.stderr.read() or ""
            except Exception:
                pass
            return (None, f"server exited (rc={proc.returncode}); stderr: {tail[-500:]}")
        # select with a short window so we can re-check the deadline
        remaining = max(0.05, deadline - time.time())
        r, _, _ = select.select([proc.stdout], [], [], remaining)
        if not r:
            continue
        try:
            chunk = proc.stdout.readline()
        except Exception as e:
            return (None, f"stdout read failed: {e}")
        if not chunk:
            # EOF
            return (None, "server closed stdout unexpectedly")
        line = chunk
        break
    if not line:
        return (None, f"no response within {timeout_s}s")
    try:
        return (json.loads(line), "")
    except json.JSONDecodeError as e:
        return (None, f"non-JSON response: {line[:200]!r} ({e})")


# ── Assertion + capture helpers ──────────────────────────────────────────────


def _substitute(template, captures: dict):
    """Substitute {name} placeholders in strings, recursively into dicts/lists."""
    if isinstance(template, str):
        def repl(m):
            key = m.group(1)
            if key in captures:
                return str(captures[key])
            return m.group(0)
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, template)
    if isinstance(template, dict):
        return {k: _substitute(v, captures) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute(v, captures) for v in template]
    return template


def _extract_json_path(root, path: str):
    """Extract by a `$.foo.bar[].baz` mini-expression. Returns None on miss.

    Supports:
      - $         → root itself
      - $.field   → dict key
      - $.a.b     → chained keys
      - $.list[]  → the list itself
      - $.list[].name → map .name over each list item
    """
    if not path.startswith("$"):
        return None
    parts = path[1:].lstrip(".").split(".") if len(path) > 1 else []
    cur = root
    for part in parts:
        if not part:
            continue
        if part.endswith("[]"):
            key = part[:-2]
            if isinstance(cur, dict):
                cur = cur.get(key)
            if not isinstance(cur, list):
                return None
            # remaining parts apply per element
            remaining_idx = parts.index(part) + 1
            if remaining_idx >= len(parts):
                return cur
            remaining = ".".join(parts[remaining_idx:])
            out = []
            for item in cur:
                v = _extract_json_path(item, f"$.{remaining}")
                if v is not None:
                    out.append(v)
            return out
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _check_subset(actual, expected: dict) -> tuple[bool, str]:
    """Subset match with the same tiny DSL as http_runner._check_subset."""
    if not isinstance(actual, dict):
        return (False, f"result is not an object (got {type(actual).__name__})")
    for key, expected_val in expected.items():
        if key not in actual:
            return (False, f"missing field '{key}' in result")
        got = actual[key]
        if isinstance(expected_val, str):
            if expected_val == "exists":
                if got is None:
                    return (False, f"field '{key}' is null (expected: any value)")
                continue
            if expected_val == "!=null":
                if got is None:
                    return (False, f"field '{key}' is null")
                continue
            if expected_val == "!=''":
                if got == "":
                    return (False, f"field '{key}' is empty string")
                continue
            matched_op = False
            for op in (">=", "<=", ">", "<"):
                if expected_val.startswith(op):
                    matched_op = True
                    try:
                        threshold = float(expected_val[len(op):])
                        got_num = float(got) if got is not None else None
                    except (TypeError, ValueError):
                        return (False, f"field '{key}': cannot compare {got!r} to {expected_val}")
                    if got_num is None:
                        return (False, f"field '{key}' is null (expected {expected_val})")
                    if op == ">=" and not (got_num >= threshold):
                        return (False, f"field '{key}' = {got_num}, expected {expected_val}")
                    if op == "<=" and not (got_num <= threshold):
                        return (False, f"field '{key}' = {got_num}, expected {expected_val}")
                    if op == ">" and not (got_num > threshold):
                        return (False, f"field '{key}' = {got_num}, expected {expected_val}")
                    if op == "<" and not (got_num < threshold):
                        return (False, f"field '{key}' = {got_num}, expected {expected_val}")
                    break
            if not matched_op and got != expected_val:
                return (False, f"field '{key}' = {got!r}, expected {expected_val!r}")
            continue
        if got != expected_val:
            return (False, f"field '{key}' = {got!r}, expected {expected_val!r}")
    return (True, "")


# ── Flow execution ───────────────────────────────────────────────────────────


def _handshake(proc: subprocess.Popen, emit) -> tuple[bool, str]:
    """Send `initialize` + `notifications/initialized`. Returns (ok, err)."""
    init_req = _rpc_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "cadillac-mcp-probe", "version": "0.1"},
    }, req_id=1)
    ok, err = _rpc_send(proc, init_req)
    if not ok:
        return (False, f"initialize send failed: {err}")
    resp, err = _rpc_recv(proc, timeout_s=5.0)
    if err or resp is None:
        return (False, f"initialize recv failed: {err or 'no response'}")
    if "error" in resp:
        return (False, f"initialize errored: {resp['error']}")
    # Send `notifications/initialized` (no id — it's a notification)
    ack = _rpc_request("notifications/initialized", {}, req_id=0,
                       is_notification=True)
    _rpc_send(proc, ack)
    return (True, "")


def run_flow(flow: MCPFlow, proc: subprocess.Popen,
             next_req_id: int) -> tuple[ProbeFailure | None, int]:
    """Execute one flow. Returns (failure_or_None, next_request_id)."""
    captures: dict = {}
    for idx, step in enumerate(flow.steps):
        params = _substitute(step.params, captures)
        req_line = _rpc_request(step.method, params, req_id=next_req_id,
                                is_notification=step.is_notification)
        summary = f"{step.method}"
        if step.method == "tools/call":
            tool_name = params.get("name", "?")
            summary = f"tools/call({tool_name})"

        ok, err = _rpc_send(proc, req_line)
        if not ok:
            return (ProbeFailure(
                probe=flow,
                failure_kind="connection_error",
                detail=f"step {idx + 1} ({summary}): {err}",
                actual=err,
            ), next_req_id)

        if step.is_notification:
            # No response expected for notifications
            next_req_id += 1
            continue

        resp, err = _rpc_recv(proc, timeout_s=step.timeout_s)
        next_req_id += 1
        if err or resp is None:
            return (ProbeFailure(
                probe=flow,
                failure_kind="timeout" if "timeout" in (err or "").lower() else "connection_error",
                detail=f"step {idx + 1} ({summary}): {err or 'no response'}",
                actual=err or "",
            ), next_req_id)

        # Error-code assertion (negative-path test)
        if step.expect_error_code is not None:
            got_err = resp.get("error", {})
            got_code = got_err.get("code") if isinstance(got_err, dict) else None
            if got_code != step.expect_error_code:
                return (ProbeFailure(
                    probe=flow,
                    failure_kind="body_mismatch",
                    detail=f"step {idx + 1} ({summary}): expected error code "
                           f"{step.expect_error_code}, got {got_code} "
                           f"(err={got_err})",
                    actual=_short(resp),
                ), next_req_id)
            continue

        # Successful-response assertions
        if "error" in resp:
            return (ProbeFailure(
                probe=flow,
                failure_kind="body_mismatch",
                detail=f"step {idx + 1} ({summary}): server returned error {resp['error']}",
                actual=_short(resp),
            ), next_req_id)

        result = resp.get("result", {})
        if step.expect_result:
            ok, why = _check_subset(result, step.expect_result)
            if not ok:
                return (ProbeFailure(
                    probe=flow,
                    failure_kind="body_mismatch",
                    detail=f"step {idx + 1} ({summary}): {why}",
                    actual=_short(resp),
                ), next_req_id)

        for cap_name, cap_path in step.capture.items():
            val = _extract_json_path(resp, cap_path)
            if val is not None:
                captures[cap_name] = val
    return (None, next_req_id)


def _short(obj) -> str:
    if obj is None:
        return ""
    try:
        return json.dumps(obj)[:400]
    except (TypeError, ValueError):
        return str(obj)[:400]


# ── Entry detection ──────────────────────────────────────────────────────────


def _detect_entry_cmd(workspace: str) -> list[str] | None:
    """Locate the MCP server entry point. Returns argv or None.

    Prefers `python3 main.py` when main.py imports the mcp SDK and looks
    like an MCP entry. Falls back to `python3 -m <package>` for
    package-style entries.
    """
    for candidate in ("main.py", "server/main.py", "src/main.py"):
        p = os.path.join(workspace, candidate)
        if not os.path.isfile(p):
            continue
        try:
            with open(p) as f:
                text = f.read()
        except OSError:
            continue
        if "mcp" in text and ("stdio" in text or "Server(" in text
                              or "FastMCP" in text or "mcp.run" in text):
            return ["python3", candidate]
    return None


# ── Strategy entry point ─────────────────────────────────────────────────────


def run(spec, workspace: str, lang, cfg, contract, emit) -> VerificationResult:
    """Run the MCP runtime verification strategy."""
    entry_cmd = _detect_entry_cmd(workspace)
    if entry_cmd is None:
        return VerificationResult(strategy="mcp", probes_run=0,
                                   skipped_reason="no MCP entry point found")

    flows = generate_flows(spec, workspace, lang, cfg, emit)
    if not flows:
        return VerificationResult(strategy="mcp", probes_run=0,
                                   skipped_reason="no flows generated")

    proc, err = _boot_server(workspace, entry_cmd, emit)
    if proc is None:
        return VerificationResult(
            strategy="mcp", probes_run=0,
            failures=(ProbeFailure(
                probe=flows[0],
                failure_kind="boot_failure",
                detail="MCP server failed to launch",
                actual=err,
            ),),
        )

    failures: list[ProbeFailure] = []
    try:
        ok, herr = _handshake(proc, emit)
        if not ok:
            emit("log", msg=f"[RUNTIME/mcp] handshake failed: {herr}")
            return VerificationResult(
                strategy="mcp", probes_run=0,
                failures=(ProbeFailure(
                    probe=flows[0],
                    failure_kind="boot_failure",
                    detail="MCP initialize handshake failed",
                    actual=herr,
                ),),
            )
        emit("log", msg="[RUNTIME/mcp] handshake OK, driving flows...")

        next_req_id = 100  # start after handshake id
        for flow in flows:
            emit("log", msg=f"[RUNTIME/mcp] flow {flow.story_id}: {flow.title}")
            fail, next_req_id = run_flow(flow, proc, next_req_id)
            if fail is not None:
                emit("log", msg=f"  [fail] {fail.failure_kind} — {fail.detail}")
                failures.append(fail)
    finally:
        _kill_group(proc)

    return VerificationResult(
        strategy="mcp",
        probes_run=len(flows),
        failures=tuple(failures),
    )
