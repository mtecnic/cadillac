# Post-Test Fixes: Config Protection, Validation Retry, note_lesson Nudge

## Context

Three stress-test builds (PawPerfect React, WS Quiz Game, TS Task Mgmt) exposed 5 issues where newly implemented features either didn't engage or existing defenses failed:

1. **`"type": "module"` anti-pattern recurrence** — Build #3 wasted 30+ iterate rounds toggling `"type": "module"` in package.json. `_protect_package_json()` runs post-scaffold and pre-validate but **not during BUILD or ITERATE**, so the LLM re-adds it and the loop spins until validation.
2. **BUILD retry ignores validation failures** — After VALIDATE fails, `retreat_to_build()` rebuilds messages, but the WORKFLOW section still says "Step 1: Run the entry point" so the LLM runs `--test`, gets exit 0, and does nothing. Budget exhausted × 3 retries with zero fixes.
3. **`note_lesson` never called** — Prompt mentions it but LLM ignores it. No nudge when stuck. Error tracker interventions don't suggest it.
4. **iterate() has zero config protections** — No calls to `_protect_package_json` or `_protect_tsconfig` anywhere in `iterate()`, so the LLM freely corrupts config files during auto-iterate rounds.
5. **Error tracker records only stderr** — Test runners (vitest, jest, pytest) write failures to stdout. `error_tracker.record()` only receives stderr, so test-loop interventions never fire.

---

## Fix 1: Real-time config protection (engine.py)

**Problem**: `_protect_package_json()` and `_protect_tsconfig()` only run at 2 phase boundaries (post-scaffold, pre-validate). During BUILD and ITERATE the LLM freely corrupts them.

**Fix**: After `process_tool_calls()` in BUILD loop and iterate loop, check if any tool call wrote to `package.json` or `tsconfig.json`. If so, run protections and inject a correction message so the LLM knows what was auto-reverted.

### Changes

**engine.py — new helper** (~line 374, after `_protect_package_json`):
```python
def _protect_config_files(workspace: str, lang, messages: list[dict], tool_msg: dict, emit) -> None:
    """Run config protections after any write to package.json/tsconfig.json."""
    if not lang or lang.family != "node":
        return
    wrote_config = False
    for tc in tool_msg.get("tool_calls", []):
        fn = tc.get("function", {})
        path = ""
        if fn.get("name") in ("write_file", "edit_file", "line_edit"):
            try:
                args = json.loads(fn.get("arguments", "{}"))
                path = args.get("path", "")
            except (json.JSONDecodeError, AttributeError):
                pass
        if "package.json" in path or "tsconfig" in path:
            wrote_config = True
            break
    if not wrote_config:
        return
    corrections = []
    if _protect_package_json(workspace, lang):
        corrections.append("Removed 'type: module' from package.json (breaks CommonJS Jest/ts-jest)")
    if _protect_tsconfig(workspace, lang):
        corrections.append("Restored tsconfig.json critical settings (module: commonjs, esModuleInterop, skipLibCheck)")
    if corrections:
        msg = "AUTO-CORRECTED: " + "; ".join(corrections) + ". Do NOT re-add these settings."
        messages.append({"role": "user", "content": msg})
        for c in corrections:
            emit("log", msg=f"[PROTECT] {c}")
```

**engine.py — BUILD loop** (~line 2440, after `process_tool_calls` and error tracking):
```python
_protect_config_files(workspace, lang, messages, msg, emit)
```

**engine.py — iterate() loop** (~line 3293, after `error_intervention` injection):
```python
_protect_config_files(workspace, lang, messages, msg, emit)
```

**engine.py — iterate() top** (~line 3055, after `lang = detect_language(...)`:
```python
# Pre-iterate: protect config files from previous damage
if lang and lang.family == "node":
    if _protect_tsconfig(workspace, lang):
        emit("log", msg="[Pre-iterate] Restored tsconfig.json")
    if _protect_package_json(workspace, lang):
        emit("log", msg="[Pre-iterate] Removed 'type: module' from package.json")
```

---

## Fix 2: Make validation failures impossible to ignore (prompts.py, engine.py)

**Problem**: When `retreat_to_build()` fires, WORKFLOW step 1 says "Run the entry point" so the LLM obeys that instead of reading the validation failures.

**Fix**: Two changes:

### A. Move validation_failures before WORKFLOW in `_BUILD_TEMPLATE` (prompts.py ~line 248)

Change the template so `{validation_failures}` appears **before** the WORKFLOW section with a hard override instruction:

```python
# In _BUILD_TEMPLATE, move {validation_failures} to before WORKFLOW:

{validation_failures}

WORKFLOW:
1. If VALIDATION FAILURES are listed above, fix ALL listed errors FIRST using edit_file — do NOT re-run the entry point until you have made fixes.
2. Run the entry point: {run_cmd} {entry_point} --test
3. Read the error output carefully
...
```

### B. Remove redundant second user message (engine.py ~line 2736-2738)

Currently after `retreat_to_build()`, a second user message repeats the failures AND says "Fix the errors... then run entry point". Replace with a direct instruction that doesn't tempt the LLM to skip to running:

```python
# engine.py ~line 2736, change from:
messages.append({"role": "user", "content":
    f"{phase_summary}\n\n{failures_text}\n\nFix the errors using the code map, then run `{lang.run_cmd} {entry_point} --test`."
})

# To:
messages.append({"role": "user", "content":
    f"{phase_summary}\n\nDo NOT run the entry point yet. Read the VALIDATION FAILURES in the system prompt and fix them with edit_file first."
})
```

---

## Fix 3: note_lesson nudge when stuck (manifest.py, engine.py)

**Problem**: `note_lesson` is in all prompt TOOLS sections but the LLM never calls it. No nudge when stuck.

**Fix**: Add a `note_lesson` suggestion to error tracker interventions and to the "no edits" nudge.

### A. Error tracker intervention (manifest.py ~line 214)

After the intervention message, append a note_lesson suggestion:

```python
# In ErrorTracker.record(), after building the intervention string (~line 219):
intervention += (
    "\n\nCall note_lesson('tried_failed', '<what you tried and why it failed>') "
    "so you don't retry this approach."
)
```

### B. "No edits" nudge in iterate() (engine.py ~line 3285)

Add note_lesson reminder to the existing nudge:

```python
nudge = (
    "You MUST call edit_file or write_file NOW. "
    "The code map has all the source code. Pick the most impactful fix and apply it. "
    "If you've been stuck, call note_lesson('tried_failed', 'what broke') first."
)
```

---

## Fix 4: Error tracker uses stdout when stderr is empty (manifest.py, engine.py)

**Problem**: `error_tracker.record()` only receives stderr, but test runners (vitest, jest, pytest) write failure output to stdout. So the error tracker never detects test-fix loops.

**Fix**: Pass stdout as fallback when stderr is empty.

### engine.py — process_tool_calls (~line 1062)

```python
# Change from:
if result.get("exit_code", 0) != 0 and result.get("stderr"):
    intervention = error_tracker.record(result["stderr"])

# To:
if result.get("exit_code", 0) != 0:
    _err = (result.get("stderr") or "").strip()
    if not _err:
        _err = (result.get("stdout") or "").strip()
    if _err:
        intervention = error_tracker.record(_err)
```

---

## Fix 5: iterate() auto-runs validation + protection before starting (engine.py)

**Problem**: `iterate()` never calls `_strip_js_extensions_from_ts()` or runs protections, so damage from prior phases persists.

Already covered in Fix 1 (pre-iterate protection block). Additionally, add `.js` extension stripping:

```python
# In iterate() top, after _protect calls:
n = _strip_js_extensions_from_ts(workspace, manifest)
if n:
    emit("log", msg=f"[Pre-iterate] Stripped .js extensions from {n} TS files")
```

---

## Files to Modify

| File | Changes |
|---|---|
| `engine.py` | New `_protect_config_files()` helper; call it in BUILD loop + iterate loop; pre-iterate protections + js-strip; fix validation retry user message (line ~2736); fix error tracker stderr→stdout fallback (line ~1062) |
| `prompts.py` | Move `{validation_failures}` before WORKFLOW in `_BUILD_TEMPLATE`; update WORKFLOW step 1 |
| `manifest.py` | Add `note_lesson` suggestion to `ErrorTracker.record()` intervention messages |

---

## Verification

1. **Unit tests**: Run existing 32 tests — should still pass (no behavior changes to scratch/memory/modules)
2. **Import smoke**: `python3 -c "from cadillac import engine, tools, prompts; print('OK')"`
3. **Config protection test**: Manually create a workspace with `"type": "module"` in package.json, call `_protect_config_files()`, verify it strips it and injects a correction message
4. **Prompt check**: Call `build_build_prompt(validation_failures="[FAIL] syntax: error TS2345...")` and verify failures appear before WORKFLOW
5. **Integration build**: Re-run Build #3 (TS Task Mgmt API) with all fixes — should NOT spiral on `"type": "module"` and should fix validation failures instead of re-running `--test`
