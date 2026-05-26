"""HTTP runtime verification — drive real user flows against a live backend.

Generates `Flow` objects from the spec + contract via the LLM, boots the
backend with the same process-group discipline `validate.py` already uses
for WIRING, and runs each flow as a chained request sequence with captures
and assertions.

Catches the bug class single-shot probes and unit tests miss:
  - logout endpoint exists, returns 200 OK, but the token still works
  - mark-done endpoint exists, returns 200 OK, but `last_completed` stays null
  - signup returns 201 on a duplicate email instead of 409
  - any "code exists, behavior wrong" gap that lives between the test suite
    and the unit-test mocks

Per flow: capture chain (signup → grab token → use it in habit-create).
Per step: substitute captures, hit live server, assert status + body shape.
First step failure short-circuits the flow but other flows continue.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .types import Probe, ProbeFailure, VerificationResult


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FlowStep:
    """One HTTP call inside a flow.

    `path` and `body` may reference captured values from prior steps via
    `{name}` substitution. `auth` is similar: `"bearer:{token}"` reads the
    `token` capture. `expect_response` supports subset-match with a tiny
    DSL: `">=N"`, `"<=N"`, `"!=null"`, `"!=''"`, or an exact value.
    `capture` extracts values from the response: `{"token": "$.token"}`
    pulls `response_json["token"]`.
    """
    method: str                          # "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
    path: str                            # "/habits/{habit_id}/complete"
    body: dict | None = None
    auth: str | None = None              # "bearer:{token}"
    expect_status: int = 200
    expect_response: dict = field(default_factory=dict)
    capture: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Flow(Probe):
    """A chain of steps that proves one story's acceptance criteria."""
    steps: tuple[FlowStep, ...] = ()


# ── LLM-driven generation ────────────────────────────────────────────────────


_GENERATE_SYSTEM_PROMPT = """You are a senior QA engineer writing executable \
HTTP user flows for an autobuilder. Given a spec of user stories and the API \
contract, output flows that prove each story actually works against a running \
server.

Output JSON only — no prose, no fences. Schema:

{
  "flows": [
    {
      "story_id": "S03",
      "title": "user logs out",
      "priority": "must",
      "steps": [
        {
          "method": "POST",
          "path": "/auth/signup",
          "body": {"email": "test@example.com", "password": "secret-pw-1234"},
          "expect_status": 201,
          "capture": {"token": "$.token"}
        },
        {
          "method": "POST",
          "path": "/auth/logout",
          "auth": "bearer:{token}",
          "expect_status": 200
        },
        {
          "method": "GET",
          "path": "/habits/",
          "auth": "bearer:{token}",
          "expect_status": 401
        }
      ]
    }
  ]
}

Rules:
- One flow per must- and should-priority story. Skip "could" stories.
- Each flow is self-contained: starts with signup/login if auth-protected, \
chains through to the action being tested.
- Use unique emails per flow (`flow1@test.com`, `flow2@test.com`, ...) so \
runs don't collide on the duplicate-email path.
- `expect_status` for the FINAL step is what proves the story. Earlier steps \
are setup — pick the expected success code.
- `expect_response` is a subset assertion against response JSON. Supports:
    - exact match: `{"streak": 1}`
    - comparison: `{"streak": ">=1", "last_completed": "!=null"}`
    - existence: `{"id": "exists"}`
  Use it on the assertion step to verify side effects, NOT just status.
- `capture` extracts response fields with `$.path` syntax (`$.token`, \
`$.data.id`, `$.user.email`).
- Use `{name}` substitution in `path`, `body` values (top-level only), and `auth`.
- Don't probe `could`-priority stories — they're nice-to-haves and would \
churn the build.
- 5-15 flows for typical apps. Don't pad.

Output ONLY the JSON object."""


def _build_generate_prompt(spec, contract, lang) -> str:
    """Compose the user message for the LLM call.

    Includes spec stories (with priorities), contract endpoints (so the LLM
    uses paths the backend actually implements), and a one-line stack hint.
    """
    stack_hint = ""
    if lang and getattr(lang, "name", "") == "python":
        stack_hint = "Stack: Python + Flask. Likely JWT bearer auth via `Authorization: Bearer <token>` header."
    elif lang and getattr(lang, "name", "") in ("typescript", "node"):
        stack_hint = "Stack: Node + Express (or similar). Likely JWT bearer auth."
    else:
        stack_hint = f"Stack: {getattr(lang, 'name', 'unknown')}."

    spec_block = spec.to_prompt_block(max_chars=4000) if spec else ""

    contract_block = ""
    if contract is not None and not contract.is_empty():
        lines = ["## API CONTRACT (use these exact paths):"]
        for ep in contract.endpoints[:30]:
            req = json.dumps(ep.request) if ep.request else "{}"
            lines.append(f"  {ep.method} {ep.path} — {ep.description or ep.name}  request={req}")
        contract_block = "\n".join(lines)

    return f"{stack_hint}\n\n{spec_block}\n\n{contract_block}\n\nGenerate flows."


def generate_flows(spec, contract, workspace: str, lang, cfg, emit) -> list[Flow]:
    """Call the LLM to produce flows. Returns a list (may be empty on error).

    Persists raw flows to `workspace/.cadillac/flows.json` so iterate/resume
    can reuse them and the user can inspect what was tested.
    """
    from cadillac.engine import chat, extract_json

    if spec is None or spec.is_empty():
        return []

    user_msg = _build_generate_prompt(spec, contract, lang)
    messages = [
        {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    emit("log", msg="[RUNTIME/http] generating flows from spec + contract...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[RUNTIME/http] LLM error: {e}; no flows generated")
        return []

    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        emit("log", msg="[RUNTIME/http] LLM did not return JSON object")
        return []
    raw_flows = parsed.get("flows") or []
    if not isinstance(raw_flows, list):
        return []

    flows: list[Flow] = []
    for entry in raw_flows:
        if not isinstance(entry, dict):
            continue
        flow = _flow_from_dict(entry)
        if flow is not None:
            flows.append(flow)

    # Persist for inspection / iterate reuse.
    try:
        cad_dir = os.path.join(workspace, ".cadillac")
        os.makedirs(cad_dir, exist_ok=True)
        with open(os.path.join(cad_dir, "flows.json"), "w") as f:
            json.dump([_flow_to_dict(fl) for fl in flows], f, indent=2)
    except OSError:
        pass

    emit("log", msg=f"[RUNTIME/http] {len(flows)} flow(s) generated")
    return flows


def _flow_from_dict(d: dict) -> Flow | None:
    try:
        story_id = str(d.get("story_id", "")).strip()
        title = str(d.get("title", "")).strip()
        priority = str(d.get("priority", "must")).strip()
        if priority not in ("must", "should", "could"):
            priority = "must"
        steps_raw = d.get("steps") or []
        if not story_id or not title or not isinstance(steps_raw, list):
            return None
        steps: list[FlowStep] = []
        for s in steps_raw:
            if not isinstance(s, dict):
                continue
            method = str(s.get("method", "GET")).upper()
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            steps.append(FlowStep(
                method=method,
                path=str(s.get("path", "/")),
                body=s.get("body") if isinstance(s.get("body"), dict) else None,
                auth=s.get("auth") if isinstance(s.get("auth"), str) else None,
                expect_status=int(s.get("expect_status", 200) or 200),
                expect_response=s.get("expect_response") if isinstance(
                    s.get("expect_response"), dict) else {},
                capture=s.get("capture") if isinstance(
                    s.get("capture"), dict) else {},
            ))
        if not steps:
            return None
        return Flow(story_id=story_id, title=title, priority=priority,
                    steps=tuple(steps))
    except (TypeError, ValueError):
        return None


def _flow_to_dict(f: Flow) -> dict:
    return {
        "story_id": f.story_id,
        "title": f.title,
        "priority": f.priority,
        "steps": [
            {
                "method": s.method, "path": s.path, "body": s.body,
                "auth": s.auth, "expect_status": s.expect_status,
                "expect_response": s.expect_response,
                "capture": s.capture,
            }
            for s in f.steps
        ],
    }


# ── Backend boot + teardown (reused infra from validate.py) ──────────────────


def _boot_backend(workspace: str, be: dict, emit) -> tuple[subprocess.Popen | None, int, str]:
    """Boot the detected backend on a free port. Returns (proc, port, error).

    Mirrors `validate.py:_wiring_dynamic_checks` boot semantics: process
    group, listening wait, env vars set. Returns (proc=None, 0, error_msg)
    on failure.
    """
    from cadillac.validate import _wiring_find_free_port

    port = _wiring_find_free_port(default=be.get("port", 5000))
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["FLASK_ENV"] = "development"
    env["FLASK_DEBUG"] = "0"
    env["PYTHONPATH"] = workspace + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Common defaults so the backend doesn't fail-fast on a missing env var
    # during verification. If the operational gate already enforces these,
    # the verifier doesn't need to bypass them again.
    env.setdefault("JWT_SECRET", "verification-secret-32chars-long-x")
    env.setdefault("DATABASE_URL", "sqlite:///:memory:")
    env.setdefault("APP_SECRET_KEY", "verification-secret-32chars-long-x")
    env.setdefault("BCRYPT_ROUNDS", "4")

    try:
        proc = subprocess.Popen(
            be["cmd"], cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
    except Exception as e:
        return (None, 0, f"failed to launch backend: {e}")

    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return (proc, port, "")
        except (ConnectionRefusedError, OSError):
            time.sleep(0.25)
        if proc.poll() is not None:
            tail = ""
            try:
                if proc.stdout:
                    tail = proc.stdout.read() or ""
            except Exception:
                pass
            return (None, 0, f"backend exited before listening (rc={proc.returncode}): {tail[-1500:]}")
    _kill_group(proc)
    return (None, 0, "backend never bound within 12s")


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
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass


# ── Flow execution ───────────────────────────────────────────────────────────


def _substitute(template: str, captures: dict) -> str:
    """Replace `{name}` placeholders with capture values. Leaves unknowns alone."""
    def repl(m):
        key = m.group(1)
        if key in captures:
            return str(captures[key])
        return m.group(0)
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, template)


def _substitute_body(body: dict | None, captures: dict) -> dict | None:
    if body is None:
        return None
    out: dict = {}
    for k, v in body.items():
        if isinstance(v, str):
            out[k] = _substitute(v, captures)
        else:
            out[k] = v
    return out


def _extract_capture(response_body, json_path: str):
    """Extract a value from a response by `$.field.subfield` path. Returns
    None if any segment is missing or the body isn't a dict."""
    if not isinstance(response_body, (dict, list)):
        return None
    if not json_path.startswith("$"):
        return None
    parts = json_path.lstrip("$.").split(".") if "." in json_path else (
        [] if json_path == "$" else [json_path.lstrip("$.")]
    )
    cur = response_body
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _check_subset(actual, expected: dict) -> tuple[bool, str]:
    """Return (ok, why) for a subset assertion against actual response JSON.

    Supports operators: `>=N`, `<=N`, `>N`, `<N`, `!=null`, `!=''`, `exists`.
    Anything else is treated as an exact-match expected value.
    """
    if not isinstance(actual, dict):
        return (False, f"response is not an object (got {type(actual).__name__})")
    for key, expected_val in expected.items():
        if key not in actual:
            return (False, f"missing field '{key}' in response")
        got = actual[key]
        if isinstance(expected_val, str):
            if expected_val == "exists":
                if got is None:
                    return (False, f"field '{key}' is null (expected: any value)")
                continue
            if expected_val == "!=null":
                if got is None:
                    return (False, f"field '{key}' is null (expected: non-null)")
                continue
            if expected_val == "!=''":
                if got == "":
                    return (False, f"field '{key}' is empty string")
                continue
            for op in (">=", "<=", ">", "<"):
                if expected_val.startswith(op):
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
            else:
                # No operator — exact string match.
                if got != expected_val:
                    return (False, f"field '{key}' = {got!r}, expected {expected_val!r}")
            continue
        # Non-string expected: exact match.
        if got != expected_val:
            return (False, f"field '{key}' = {got!r}, expected {expected_val!r}")
    return (True, "")


def _run_step(step: FlowStep, captures: dict, port: int) -> tuple[int, object, str]:
    """Execute one step. Returns (status, response_body, error_text)."""
    path = _substitute(step.path, captures)
    body = _substitute_body(step.body, captures)
    headers = {}
    if step.auth and step.auth.startswith("bearer:"):
        token_template = step.auth[len("bearer:"):]
        token = _substitute(token_template, captures)
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"

    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=step.method,
                                   headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8", errors="replace")
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
    except urllib.error.URLError as e:
        return (0, None, f"connection error: {e.reason}")
    except (TimeoutError, socket.timeout):
        return (0, None, "request timed out (10s)")
    except Exception as e:
        return (0, None, f"request crashed: {e}")

    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw  # non-JSON body — caller can still check status
    return (status, parsed, "")


def run_flow(flow: Flow, port: int) -> ProbeFailure | None:
    """Execute one flow start to finish. Returns the first failure, or None."""
    captures: dict = {}
    for idx, step in enumerate(flow.steps):
        status, body, err = _run_step(step, captures, port)
        request_summary = f"{step.method} {_substitute(step.path, captures)}"

        if err:
            return ProbeFailure(
                probe=flow,
                failure_kind="connection_error" if "connection" in err else "timeout",
                detail=f"step {idx + 1} ({request_summary}): {err}",
                actual=err,
            )
        if status != step.expect_status:
            return ProbeFailure(
                probe=flow,
                failure_kind="status_mismatch",
                detail=f"step {idx + 1} ({request_summary}): expected status "
                       f"{step.expect_status}, got {status}",
                actual=_short(body),
            )
        if step.expect_response:
            ok, why = _check_subset(body, step.expect_response)
            if not ok:
                return ProbeFailure(
                    probe=flow,
                    failure_kind="body_mismatch",
                    detail=f"step {idx + 1} ({request_summary}): {why}",
                    actual=_short(body),
                )
        # Captures (only on success)
        for cap_name, cap_path in step.capture.items():
            val = _extract_capture(body, cap_path)
            if val is not None:
                captures[cap_name] = val
    return None


def _short(body) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body[:400]
    try:
        return json.dumps(body)[:400]
    except (TypeError, ValueError):
        return str(body)[:400]


# ── Strategy entry point ─────────────────────────────────────────────────────


def run(spec, workspace: str, lang, cfg, contract, emit) -> VerificationResult:
    """Run the HTTP runtime verification strategy."""
    from cadillac.validate import _wiring_detect_backend

    be = _wiring_detect_backend(workspace)
    if be is None:
        return VerificationResult(strategy="http", probes_run=0,
                                   skipped_reason="no backend detected at run time")

    flows = generate_flows(spec, contract, workspace, lang, cfg, emit)
    if not flows:
        return VerificationResult(strategy="http", probes_run=0,
                                   skipped_reason="no flows generated")

    proc, port, err = _boot_backend(workspace, be, emit)
    if proc is None:
        return VerificationResult(
            strategy="http", probes_run=0,
            failures=(ProbeFailure(
                probe=flows[0],  # attribute the boot failure to the first flow
                failure_kind="boot_failure",
                detail="backend boot failed before any flow could run",
                actual=err,
            ),),
        )

    failures: list[ProbeFailure] = []
    try:
        for flow in flows:
            emit("log", msg=f"[RUNTIME/http] flow {flow.story_id}: {flow.title}")
            fail = run_flow(flow, port)
            if fail is not None:
                emit("log", msg=f"  [fail] {fail.failure_kind} — {fail.detail}")
                failures.append(fail)
    finally:
        _kill_group(proc)

    return VerificationResult(
        strategy="http",
        probes_run=len(flows),
        failures=tuple(failures),
    )
