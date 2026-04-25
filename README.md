# Cadillac

> An autonomous LLM agent that takes a natural-language task and produces a validated application. No humans in the loop.

```bash
$ python3 -m cadillac --api-url http://192.168.86.39:8000/v1 --plain auto \
    "TypeScript Express REST API for a todo list, 3 endpoints, Jest tests, SQLite"

[PLAN (Round 1/4)]  ...
[DEPS (Round 1/3)]  [OK] npm install (12 packages)
[SCAFFOLD]          writing 8 files, batch 1/1
[BUILD]             tests fail → fix cycle → edit_file src/routes.ts
[VALIDATE]          [PASS] syntax  [PASS] lint  [PASS] tests (12/12)  ...  [PASS] run
[AUTO] All validations pass!

COMPLETE | 42 rounds | 8 files | 141.5s
```

That's a real run transcript. Cadillac builds the thing, runs the tests, fixes the failures, and ships.

---

## What it does

Give it a task. It writes the whole application.

| Input | Output |
|---|---|
| `"pygame snake game with arrow controls and pytest"` | 13-file modular game, passes 6 validations, playable |
| `"Flask REST API for bookmarks, SQLite, pytest"` | 5 endpoints, database layer, 15+ passing tests |
| `"ASCII FPS in the terminal"` | 32-file modular codebase, 10 modules, 94 rounds, 17 min — [shipped to GitHub](https://github.com/mtecnic/matrix-doom) |
| `"Full-stack recipe book: Flask + React + Vite + Vitest + Pytest"` | 38 files across `backend/` and `frontend/`, 7 of 8 validations pass, real TDD iteration |

The agent plans. It installs dependencies. It scaffolds files in dependency order. It runs the tests. When tests fail, it reads the failure, edits the code, and runs them again. When it's done, it packages the project with a README and requirements.

---

## The pipeline

```
      PLAN ── DEPS ── SCAFFOLD ── REVIEW ── BUILD ── INTEGRATE ── VALIDATE ── PACKAGE
       │       │         │          │        │         │            │          │
     arch   npm/pip   stub files  critic  fix cycle  glue       8-check     README +
     plan   install              findings            phase      pipeline   requirements
```

**Flat pipeline** (<15 files): one agent, one manifest. Everything happens in a single phase loop.

**Modular pipeline** (15+ files): plan is decomposed into modules sorted by dependency. Each module gets its own scoped executor (can only write to its dir), its own scratch, its own code-map. Modules build in dependency waves; later waves see interfaces extracted from earlier waves via AST parsing.

Both pipelines share the same phase state machine, adaptive budgets, memory, and validation.

---

## The validation pipeline

Every build ends in the same 8-check gate:

| Check | What it does |
|---|---|
| **syntax** | `py_compile` / `tsc --noEmit` / `node --check` per file |
| **imports** | Every `import` resolves to stdlib, declared dep, or local module |
| **lint** | `ruff` / `eslint` (auto-configured with safe rules) |
| **framework** | Project-specific sanity: Flask routes registered? React app.mount? |
| **functional** | Build artifact check: `vite build` exits clean, bundle produced |
| **run** | Entry point executes without crashing on a smoke-test invocation |
| **tests** | `pytest` / `vitest` / `jest` all pass |
| **naming** | Plan's declared module names match the filesystem |

Fails trigger retreat-to-BUILD, up to `max_validate_retries` cycles. When all validations pass, `PACKAGE` writes the README and `requirements.txt`/`package.json`.

---

## Why this isn't just "call GPT in a loop"

Cadillac is built on one principle that shapes every design decision:

> **Every value should be a consequence, not a policy.**

If a knob's value can be computed from something cadillac already sees — the plan, the model, the host, past builds — then it should be, and there should be no CLI flag for it. No config file either. The harness figures it out.

Concretely:

| Old approach | Cadillac approach |
|---|---|
| `--timeout 30` on every command | `_adaptive_timeout(cmd, workspace)` reads `.cadillac/cmd_history.jsonl`, returns `max(baseline, p95 × 2.5)` clamped to 600s |
| `--context-size 40000` | `--context-auto` probes `/v1/models`, deducts 20k+5% headroom, feeds `compute_context_budget` |
| Hardcoded `READ_LIMIT = 16000` chars | `compute_read_limits(cfg.max_context_tokens)` scales to 15% of the model window |
| Static `APPROVED_VERSIONS = {"vitest": "^1"}` | `approved_versions_for_host()` runs `node --version` once, maps Node 18→^1, 20→^2, 22→^3 |
| Fixed `BUILD = 30` rounds | `compute_budgets(plan, task_text)` consults `phase_budgets.jsonl` for past p90 × 1.2 on matching tags |
| Pin pip `--break-system-packages` via docs | Detects `/usr/lib/python3*/EXTERNALLY-MANAGED`, auto-inserts the flag |

If you find yourself adding a CLI flag to cadillac, the usual answer is "derive it from a signal you're already reading."

---

## Memory

Cadillac accumulates two kinds of persistent memory across runs:

**`memory.jsonl`** — lessons. Every time reflection fires (success or failure), it extracts `{type, trigger, fix, tags, polarity, confidence}` entries. Before each build, `recall(task)` scores them by tag-filter + keyword overlap + recency + confidence and injects the top 10 into the system prompt. Lessons whose triggers appear in fresh errors lose confidence (`penalize_backfired`); lessons that survive apply successfully gain it.

**`phase_budgets.jsonl`** — rounds per phase per build. After each build, `record_phase_outcome(phase, rounds, n_files, tags)` appends one row per phase. Next time a build with matching tags runs, `compute_budgets` uses the p90 of those rounds × 1.2 as the budget floor. Modular builds roll up per-module rounds (via `_build_module_wave`) so the real work shows up in history, not a fake top-level "1 round."

Both are plain JSONL files. Inspect them. Grep them. Delete them to start fresh.

---

## Tool API (LLM's side)

The LLM drives the build through a sandboxed tool layer. Each tool is a bounded, observable operation:

| Tool | Purpose |
|---|---|
| `read_file` / `write_file` / `edit_file` / `line_edit` | Sandboxed filesystem access with path validation and manifest tracking |
| `list_files` | Directory listing scoped to the workspace |
| `run_command` | Shell execution with adaptive timeout, cmd_history recording, LLM path sanitization (strips `/testbed`, preserves real subdir `cd`), PEP 668 auto-flag |
| `add_dep(name, version, dev)` | Only sanctioned post-scaffold package.json write. Policy-gated via `inspector.coerce_version`. |
| `note_lesson(category, content)` | Within-build scratchpad the LLM writes to; re-injected into system prompt every 3 rounds |
| `check_status` | Current phase, round, file count, validation mix |

The `ToolExecutor` runs inside a `ModuleScopedExecutor` during modular builds — it can only touch its own module's files. Post-scaffold, configs (`package.json`, `tsconfig.json`, `vite.config.*`, `jest.config.*`) are frozen except through `add_dep` and cadillac's own self-healing helpers.

---

## What's in the box

```
cadillac/
├── engine.py         ~3100 lines — phase state machine, chat loop, modular orchestration
├── modules.py        ModuleSpec, ModularPlan, dependency topo sort, cycle detection
├── prompts.py        LLM templates for each phase (flat + modular variants)
├── tools.py          ToolExecutor + ModuleScopedExecutor, sandboxed I/O
├── validate.py       The 8-check pipeline, per-language validators
├── phases.py         Phase enum, budgets, PhaseState, retry logic
├── manifest.py       Thread-safe file registry with summaries
├── inspector.py      3-tier building-code enforcement (materials/wiring/commissioning)
├── codemap.py        Tiered source representation (AST tier 1 → regex tier 2 → raw tier 3)
├── memory.py         Lesson accumulation, tag-aware recall, phase-budget history
├── scratch.py        Per-module within-build scratch files
├── languages.py      Python + TypeScript strategy (react/vue/angular detection)
├── quality.py        Coding standards, anti-patterns, few-shot examples
├── display.py        Rich live terminal UI (file tree, activity log, validation)
├── events.py         Structured events decoupling engine from display
├── progress.py       progress.md writer + compact LLM context
├── cli.py            Interactive shell, workspace resolution
├── cadillac.py       CLI entry point (argparse)
├── memory.jsonl      Accumulated lessons — 60+ entries
├── phase_budgets.jsonl   Cross-build phase-round history
└── tests/            269 unit tests covering all of the above
```

---

## Quick start

```bash
cd /home/waive3/sandbox
python3 -m cadillac --api-url http://<your-openai-compatible-endpoint>/v1 --plain auto \
    "your task here"
```

The LLM backend can be any OpenAI-compatible endpoint. This project develops against a local vLLM server on the LAN (Qwen3-Coder), but OpenAI and Anthropic endpoints work too — cadillac auto-detects the context window and model identity from `/v1/models`.

### Common commands

```bash
python3 -m cadillac --api-url $URL --plain auto "task"         # build + auto-iterate
python3 -m cadillac --api-url $URL --plain new "task"          # just build (no iterate)
python3 -m cadillac iterate workspace-XXXXXXXX "instruction"   # extend an existing build
python3 -m cadillac resume workspace-XXXXXXXX                  # resume a crashed build
python3 -m cadillac enhance ./my-project "add auth middleware" # modify external codebase
python3 -m cadillac debug workspace-XXXXXXXX syntax            # focused debug pass
python3 -m cadillac list                                       # show all workspaces
python3 -m cadillac                                            # interactive shell
```

### Flags worth knowing

| Flag | What |
|---|---|
| `--plain` | Disable Rich UI (required when stdout is piped/redirected) |
| `--context-auto` | Probe `/v1/models` for the model's context window, budget accordingly |
| `--rate` | Client-side rate-limit RPS (default `0.25`, 0 disables) |
| `--parallel` | Build independent modules in parallel waves (max 3) |
| `--max-iterations` | Outer auto-iterate cycles after first validation (default 3) |

All other tuning is automatic. That's by design.

---

## Real builds

### Matrix Doom — first modular build, shipped to GitHub

32 files. 10 modules. 94 rounds. 17 minutes. Zero human edits. An ASCII FPS with a game loop, raycasting renderer, wall-collision, enemies, HUD.

[https://github.com/mtecnic/matrix-doom](https://github.com/mtecnic/matrix-doom)

### Pygame Snake — modular

13 files. 3 modules (`core`, `renderer`, `main`). pytest tests for the game-logic module only (no pygame imports). All validations pass. BUILD rolled up 49 rounds across modules.

### Full-stack Recipe App — stress test

38 files across `backend/` (Flask + SQLite + pytest) and `frontend/` (React + Vite + Vitest). 596 rounds. 115 minutes. 7 of 8 validations pass at final (only `run` fails on an `.tsx` entry-point edge case — see known limits). Exposed and drove fixes for 10+ cadillac framework bugs in the process.

### TypeScript Express API

Flat build. 8 files. 12 Jest tests. All 8 validations pass. ~2 min.

---

## Design notes

### The phase loop

`run()` in `engine.py` is a single `while True` over phases. Each iteration calls `state.tick()` (increments the round counter), checks global budget, picks a phase handler, runs one LLM turn, processes tool calls, and decides whether to advance. Wrapped in try/except for `EndpointUnreachable` (LLM endpoint permanently down) and `KeyboardInterrupt` (SIGTERM or Ctrl+C). Both unwind to an end-of-run block that writes phase history and closes the build log — so a killed partial build still contributes data to the next build's budget computation.

### The inspector

Three tiers of defense against known LLM failure modes:

1. **Materials** — dependency version check (no `"*"` on critical packages, no versions above the host's Node major).
2. **Wiring** — does `package.json.scripts.test` point at a real runner? Is the entry point declared in `plan.json` present on disk? Are there any orphan config files?
3. **Commissioning** — does the entry point actually load without throwing? Does `--test` dispatch to the test runner? Does the project compile?

Runs at phase boundaries. Violations become LLM-readable injection blocks in the next prompt.

### Self-healing

Post-scaffold, certain files and patterns are protected against LLM re-corruption:

- `package.json` `"type": "module"` — context-aware strip. Removed for CJS-Jest setups where it breaks ts-jest. Preserved for Vite/Vitest/React/Vue where it's required to silence the CJS-deprecation warning.
- `tsconfig.json` — `rootDir` stripped (recurring bug), `skipLibCheck` ensured.
- `jest.config.*` / `vite.config.*` / `vitest.config.*` / `tsconfig.json` — frozen after scaffold. Writes via `write_file` or shell redirect are blocked; changes only through `add_dep` or cadillac's own safeguard helpers.
- `.js` extensions in TS imports are stripped (`from './foo.js'` → `from './foo'`) because Qwen models emit them for CommonJS TS, which breaks resolution.

### The modular pipeline's scoped executors

Each module gets a `ModuleScopedExecutor` that's allowed to `write_file`, `edit_file`, and `read_file` only within its own module directory. `run_command` is shared but the working directory is scoped. This prevents one module's build from accidentally editing another module's files, which LLMs will cheerfully do otherwise. Cross-module visibility happens through `get_dependency_interfaces()` — AST-extracted function/class signatures from already-built upstream modules get injected into the downstream module's prompt.

---

## Known limits

- **The LLM has to be smart enough.** On a weak model, hard test mocks (vitest `vi.mock`), TypeScript union type narrowing, async timing, and React test-library queries with multiple matchers will stump the loop. Cadillac gives the LLM every tool it needs — memory, scratch, code map, error logs, stuck-pattern detection — but it can't substitute for reasoning capacity.
- **Node execution of `.tsx` entry points.** For React projects, cadillac's `run` check uses `vite build` correctly, but the fallback `node entry_point --test` path triggers `ERR_UNKNOWN_FILE_EXTENSION` on `.tsx`. This surfaces occasionally when language state mutates mid-build; see `validate.py` `_check_run_ts`.
- **Plan-driven subdirs.** If the plan's `entry_point` is `src/main.tsx` but the LLM self-organizes as a full-stack with `backend/` and `frontend/` subdirs, the root boilerplate + LLM's frontend-subdir code can end up with duplicate `src/` layouts. Resolved per-build by the inspector's wiring check flagging the mismatch; worth a deeper fix.
- **Headless only.** The `waive3` dev machine is headless, so pygame/GUI apps get built and tested (with `SDL_VIDEODRIVER=dummy`) but never visually verified.
- **Single backend endpoint.** Cadillac clients against one LLM endpoint per invocation. Fleet/multi-model routing isn't built in.

---

## Development

```bash
# Run the test suite
python3 -m unittest cadillac.tests.test_post_test_fixes \
    cadillac.tests.test_scratch \
    cadillac.tests.test_memory \
    cadillac.tests.test_modules

# 269 tests, ~2 seconds

# Import-check after edits
python3 -c "from cadillac import engine"
```

Workspaces land in `$CWD/workspace-YYYYMMDD-HHMMSS/`. Each has a `.cadillac/` dir with `build.jsonl` (append-only event log), `cmd_history.jsonl` (adaptive timeout data), `scratch.md` (within-build notes), and `checkpoint.json` (resume state).

To pair cadillac against a new LLM endpoint, no config file is required — just pass `--api-url` and optionally `--model`. Context window, model identity, and cross-call pacing are all auto-detected.

---

## Philosophy, one more time

Cadillac is not trying to be a general-purpose agent framework. It is an opinionated harness for one specific job — *take a task description, produce a validated application* — and every design choice bends toward making that job converge more often, faster, with less intervention.

If you catch cadillac pushing a decision back to the user when it could have been inferred from a signal cadillac already has, that's a bug. File it.
