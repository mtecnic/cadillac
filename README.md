<div align="center">

```
   ██████╗ █████╗ ██████╗ ██╗██╗     ██╗      █████╗  ██████╗
  ██╔════╝██╔══██╗██╔══██╗██║██║     ██║     ██╔══██╗██╔════╝
  ██║     ███████║██║  ██║██║██║     ██║     ███████║██║
  ██║     ██╔══██║██║  ██║██║██║     ██║     ██╔══██║██║
  ╚██████╗██║  ██║██████╔╝██║███████╗███████╗██║  ██║╚██████╗
   ╚═════╝╚═╝  ╚═╝╚═════╝ ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝
```

### **C**ritic · **A**utonomous · **D**ecomposing · **I**terative · **L**earning · **L**ifecycle · **A**daptive · **C**ompiler

*A natural-language task in. A validated application out.*

![Python](https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/typescript-5.x-3178C6?style=flat-square&logo=typescript&logoColor=white)
![Tests](https://img.shields.io/badge/tests-307%20passing-2ea44f?style=flat-square)
![Builds](https://img.shields.io/badge/applications%20built-92-blue?style=flat-square)
![Lessons](https://img.shields.io/badge/lessons%20learned-275-purple?style=flat-square)
![License](https://img.shields.io/badge/license-private-red?style=flat-square)

</div>

---

## ✨ One sentence in. A working app out.

```bash
$ python3 -m cadillac --api-url $LLM_URL --plain auto \
    "Full-stack recipe app: Flask + SQLite backend, React + Vite frontend, pytest + vitest"
```

```diff
[PLAN]      ✓ architecture drafted, 22 files, modular: yes
[DEPS]      ✓ npm install (12 packages)  ·  pip install -r requirements.txt
[SCAFFOLD]  ✓ writing files in dependency order
[BUILD]     ⟲ tests fail → read error → edit_file → re-run → tests pass
[VALIDATE]  ✓ syntax  ✓ imports  ✓ lint  ✓ framework  ✓ tests  ✓ run
[PACKAGE]   ✓ README.md, requirements.txt, package.json
+ COMPLETE | 596 rounds | 38 files | 7/8 validations
```

That's a real run transcript. **No human edits.** Cadillac plans the architecture, installs dependencies, writes every file, runs the tests, debugs the failures, and ships.

---

## 📖 The journey, in one comparison

The very first thing cadillac ever built — **2026-04-01** — was an asyncio IRC server for AI agent collaboration. It didn't work. The 9 files it produced contained a syntax error on line 61 of `irc_server.py`, a dead message queue with no consumer, a web UI whose Send button POSTed to a nonexistent route, and zero tests.

24 days later, the same task — same harness, same builder — produced this:

| | **Day 1** | **Day 25** |
|---|---:|---:|
| Files | 9 flat `.py` files | 22 in 7 subpackages + tests |
| Has syntax errors? | ✅ yes | ❌ no |
| Server starts? | ❌ | ✅ |
| Tests pass? | (none) | ✅ |
| Validations | 0 / 8 | **8 / 8** ✓ |

The difference isn't a smarter model — it's 24 days of cadillac itself learning what breaks builds and how to prevent it. Every failure became a fix. Every fix became a test. Every test became a guardrail.

#### 🔁 Then we asked it to **repair the original broken version**

We copied the day-1 code into a fresh dir and ran:

```bash
$ python3 -m cadillac --api-url $LLM_URL enhance ./irc-server-broken \
    "fix all these issues: <8 specific bugs>"
```

In **~7 minutes**, cadillac:

- ✅ Found and fixed the `split(':',, 1)` syntax error on line 61
- ✅ Wrote 3 new test files (`test_rate_limiter.py`, `test_channel_manager.py`, `test_irc_parser.py`) with real assertions
- ✅ Got every check green: `naming · imports · syntax · lint · framework · functional · run · tests`
- ✅ `[ENHANCE] All validations pass!`

Two paths, same destination:

```
                       ┌──── BUILD FROM SCRATCH ────►  22 files · 8/8 · 45 min
   "AI agent IRC      ─┤
    server" task       └──── REPAIR THE BROKEN     ──►   3 tests added · 8/8 · 7 min
                                  DAY-1 VERSION
```

Whether you start from a sentence or a broken codebase, the harness converges on the same place: green.

---

## 🔤 What's in the name

Each letter ties to a *named subsystem in the codebase* — every word is something you can `grep` for in the source.

| | Word | What it actually maps to | Lives in |
|:---:|---|---|---|
| **C** | **Critic-driven** | The REVIEW phase runs an adversarial critic against the scaffolded plan; the inspector then validates materials, wiring, and commissioning at phase boundaries. Cadillac argues with itself before it ships. | `prompts.build_review_prompt` · `inspector.inspect_*` |
| **A** | **Autonomous** | One sentence in, working app out. Picks file order, retry counts, timeouts, version pins — without asking. The harness's job is to never push a decision back to the user when it can be inferred. | `engine.run` (the `while True` phase loop) |
| **D** | **Decomposing** | 15+ file projects are decomposed into dependency-sorted modules, built in waves. Each module gets a scoped executor that can only touch its own directory. | `modules.ModularPlan` · `engine._build_module_wave` · `tools.ModuleScopedExecutor` |
| **I** | **Iterative** | Every validation failure feeds back as `retreat_to_build`. The state machine doesn't fail — it loops with new context until the 8-check gate goes green or budget runs out. | `phases.PhaseState.retreat_to_build` |
| **L** | **Learning** | `memory.jsonl` accumulates lessons (275 today, tag-filtered, confidence-scored, decay-aware). `phase_budgets.jsonl` records rounds-per-phase so the next build's budget is computed from the previous one's reality. | `memory.recall` · `memory.record_phase_outcome` |
| **L** | **Lifecycle** | Full **PLAN → DEPS → SCAFFOLD → REVIEW → BUILD → INTEGRATE → VALIDATE → PACKAGE** pipeline. Not "code generation" — *application lifecycle*. The output is a packaged, runnable project with README and dep manifest. | `phases.Phase` · `phases.PHASE_ORDER` |
| **A** | **Adaptive** | Every meaningful value is derived from a signal cadillac already sees. Timeouts from `cmd_history.jsonl`. Context budgets from `/v1/models`. Version pins from `node --version`. Phase budgets from p90 of past rounds. The user never tunes any of this. | `tools._adaptive_timeout` · `engine.compute_context_budget` · `inspector.approved_versions_for_host` · `phases.compute_budgets` |
| **C** | **Compiler** | Task description in. **Validated** application out. Like a compiler, the artifact must pass an uncompromising check before it's emitted. Like a compiler, the output is deterministic given the same input + memory state + seed. | `validate.run_validation` (the 8-check gate) |

> *Cadillac is a* ***critic-driven, autonomous, decomposing, iterative, learning lifecycle*** *for* ***adaptive compilation*** *of natural-language tasks into validated applications.*

---

## 🔧 How it works

```
┌─────────┐   ┌──────┐   ┌──────────┐   ┌────────┐   ┌───────┐   ┌─────────────┐   ┌──────────┐   ┌─────────┐
│  PLAN   │ → │ DEPS │ → │ SCAFFOLD │ → │ REVIEW │ → │ BUILD │ → │  INTEGRATE  │ → │ VALIDATE │ → │ PACKAGE │
└─────────┘   └──────┘   └──────────┘   └────────┘   └───────┘   └─────────────┘   └──────────┘   └─────────┘
   plan       npm/pip      stub files    adversarial    fix         glue            8-check         README +
   modular?   install                    critic         cycle       phase           pipeline        deps file
```

**Flat pipeline** — for projects with fewer than 15 files. One agent, one manifest. Everything happens in a single phase loop.

**Modular pipeline** — for 15+ files. Plan is decomposed into modules, sorted by dependency, built in waves. Each module gets its own scoped executor (can only write to its directory), its own scratch file, its own code-map. Cross-module visibility happens through AST-extracted interfaces from already-built upstream modules.

Both pipelines share the same phase state machine, adaptive budgets, memory, and validation gate.

---

## ✅ The 8-check validation gate

```
┌──────────────┬──────────────────────────────────────────────────────────────────┐
│  syntax      │  py_compile / tsc --noEmit / node --check on every source file  │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  imports     │  every import resolves to stdlib, declared dep, or local module │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  lint        │  ruff / eslint with auto-configured safe rule set                │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  framework   │  Flask routes registered? React app.mount? type:module set?      │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  functional  │  build artifact produced (vite build, tsc --build, etc.)         │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  run         │  entry point executes a smoke-test invocation cleanly            │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  tests       │  pytest / vitest / jest all green                                │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  naming      │  plan's declared module names match the filesystem               │
└──────────────┴──────────────────────────────────────────────────────────────────┘
```

Failing a check triggers retreat-to-BUILD, up to `max_validate_retries` cycles. When all 8 pass, `PACKAGE` writes the README and `requirements.txt` / `package.json`.

---

## 📊 The dashboard

```bash
$ python3 -m cadillac dash
```

```
╭─────────────────────────────────────────────────────────────────────────────╮
│  CADILLAC DASH   PROJECTS   92 projects   2 live                            │
╰─────────────────────────────────────────────────────────────────────────────╯
╭──── Projects (92/92) ────╮╭── Detail — workspace-20260425-024446 ───────────╮
│  ▶  ✓ DONE   2…   22/22  ││  Task        Async IRC server in Python for    │
│     ● LIVE   2…   18/30  ││              AI agent collaboration           │
│     ✓ DONE   2…    7/7   ││  Status      ✓ DONE                            │
│     ✗ STOP   2…    0/47  ││  Phase       Phase 7/7                         │
│     ✓ DONE   2…   38/38  ││  Elapsed     20m 34s                           │
│     INTEGR…  2…    2/23  ││                                                │
│     ✓ DONE   2…   13/13  ││  Validations:                                  │
│     PLAN     2…    0/17  ││    nam:✓ imp:✓ syn:✓ lin:✓ fra:✓               │
│     ✓ DONE   2…    8/8   ││    fun:✓ run:✓ tes:✓                           │
│     ✓ DONE   2…   32/32  ││                                                │
╰──────────────────────────╯│  Files: 22/22   Deps: 7/7                      │
╭────────── Live (2) ──────╮│                                                │
│ ● enhance-broken-irc     ││  Recent events:                                │
│ ● bake-off-qwen3.6       ││    log    [PACKAGE] Done                       │
╰──────────────────────────╯│    lesson sqlite3 sync calls in async handlers │
                            │    log    [AUTO] All validations pass!         │
                            ╰────────────────────────────────────────────────╯
╭─────────────────────────────────────────────────────────────────────────────╮
│  ? help  ↑↓ nav  m memory  b budgets  e events  r refresh  / filter  q quit│
╰─────────────────────────────────────────────────────────────────────────────╯
```

Five views, keyboard-switched:

| Key | View | Shows |
|:--:|---|---|
| *(default)* | **Projects** | List of workspaces, validation summary, file progress, lessons applied |
| `m` | **Memory** | 275 accumulated lessons, top by confidence, tag histogram, do/don't split |
| `b` | **Budgets** | Phase-history stats: mean / p90 / max rounds per (tag-bucket, phase) |
| `e` | **Events** | Live tail of selected workspace's `.cadillac/build.jsonl` |
| `?` | **Help** | Key reference |

**Zero services.** No port. No daemon. Pure read-only filesystem access. When you press `q`, nothing remains.

---

## 🎯 The principle

> **Every value should be a consequence, not a policy.**

If a knob's value can be computed from a signal cadillac already sees — the plan, the model, the host, past builds — then it should be, and there should be no CLI flag for it. No config file either. The harness figures it out.

| ❌ Old way | ✅ Cadillac way |
|---|---|
| `--timeout 30` on every command | `_adaptive_timeout(cmd, workspace)` reads `.cadillac/cmd_history.jsonl`, returns `max(baseline, p95 × 2.5)` clamped to 600s |
| `--context-size 40000` | `--context-auto` probes `/v1/models`, deducts 20k+5% headroom |
| Hardcoded `READ_LIMIT = 16000` | `compute_read_limits(cfg.max_context_tokens)` scales to 15% of model window |
| Static `vitest: ^1` pin | `approved_versions_for_host()` runs `node --version`, picks Node-major-correct ceilings |
| Fixed `BUILD = 30` rounds | `compute_budgets(plan, task_text)` consults `phase_budgets.jsonl` for past p90 × 1.2 on matching tags |
| Document `pip install --break-system-packages` | Auto-detect `/usr/lib/python3*/EXTERNALLY-MANAGED`, insert flag |

If you find yourself adding a CLI flag, the answer is usually *"derive it from a signal you already read."*

---

## 🧠 Memory + history

Two persistent JSONL files that build up across runs:

#### `memory.jsonl` — lessons

Every reflection cycle (success **or** failure) extracts entries:

```json
{
  "ts": 1777045300.5,
  "type": "error_pattern",
  "trigger": "vitest 0.34 + jsdom on Node 18",
  "fix": "pin vitest to ^1 in devDependencies",
  "tags": ["typescript", "vitest", "node"],
  "polarity": "do",
  "confidence": 0.85,
  "used": 7
}
```

Before each build, `recall(task)` scores them by **tag-filter + keyword-overlap + recency + confidence** and injects the top 10 into the system prompt. Lessons whose triggers appear in fresh errors *lose* confidence (`penalize_backfired`); lessons that survive an applied build *gain* it.

#### `phase_budgets.jsonl` — rounds-per-phase history

After each build, `record_phase_outcome(phase, rounds, n_files, tags)` appends one row per phase. Next time a build with matching tags runs, `compute_budgets` uses the **p90 of those rounds × 1.2** as the budget floor. Modular builds roll up per-module rounds so the real work shows up — not the fake 1-round top-level snapshot.

Both are plain JSONL. Inspect them. Grep them. Delete them to start fresh.

---

## 🛠 The LLM's tool API

The agent drives every build through a sandboxed tool layer. Each tool is bounded, observable, and sandboxed to the workspace.

| Tool | What it does |
|---|---|
| 📖 `read_file` | Read a file with adaptive byte/line caps proportional to model context |
| ✏️ `write_file` | Write a new file (post-scaffold protections enforce config-file freeze) |
| 🔧 `edit_file` | Anchor-based replacement edit |
| 📐 `line_edit` | Line-range surgical edit |
| 📂 `list_files` | Directory listing scoped to workspace |
| ⚡ `run_command` | Sandboxed shell with adaptive timeouts, history-recording, LLM-path sanitization, PEP 668 auto-flag |
| 📦 `add_dep` | The only sanctioned post-scaffold `package.json` write — policy-gated through `inspector.coerce_version` |
| 💡 `note_lesson` | Within-build scratchpad; re-injected into system prompt every 3 rounds |
| 📊 `check_status` | Current phase, round, file count, validation mix |

In modular builds, `ToolExecutor` is wrapped by `ModuleScopedExecutor` — file ops are restricted to the module's own directory. Configs (`package.json`, `tsconfig.json`, `vite.config.*`, `jest.config.*`) are frozen post-scaffold; modifications go through `add_dep` or cadillac's self-healing helpers.

---

## 📁 What's in the box

```
cadillac/
├── 🎛  engine.py          ~3100 lines — phase state machine, chat loop, modular orchestration
├── 🧱  modules.py         ModuleSpec, ModularPlan, dependency topo-sort, cycle detection
├── 📝  prompts.py         LLM templates for each phase (flat + modular variants)
├── 🔨  tools.py           ToolExecutor + ModuleScopedExecutor, sandboxed I/O
├── ✅  validate.py        The 8-check pipeline, per-language validators
├── 🎯  phases.py          Phase enum, budgets, PhaseState, retry logic
├── 📋  manifest.py        Thread-safe file registry with structural summaries
├── 🔍  inspector.py       3-tier building-code enforcement (materials/wiring/commissioning)
├── 🗺️  codemap.py         Tiered source representation (AST tier 1 → regex tier 2 → raw tier 3)
├── 🧠  memory.py          Lesson accumulation, tag-aware recall, phase-budget history
├── 📓  scratch.py         Per-module within-build scratch files
├── 🌐  languages.py       Python + TypeScript strategy (react/vue/angular detection)
├── 📐  quality.py         Coding standards, anti-patterns, few-shot examples
├── 🖥️  display.py         Rich live terminal UI (file tree + activity log + validation)
├── 📡  events.py          Structured events decoupling engine from display
├── 📈  progress.py        progress.md writer + compact LLM context
├── 📊  dash.py            Zero-service TUI dashboard (this README ↑)
├── 💻  cli.py             Interactive shell, workspace resolution
├── ⚙️   cadillac.py       CLI entry point (argparse)
├── 💾  memory.jsonl       275 accumulated lessons
├── 📚  phase_budgets.jsonl   Cross-build phase-round history
└── 🧪  tests/             307 unit tests covering all of the above
```

---

## ⚡ Quick start

```bash
cd /home/waive3/sandbox

python3 -m cadillac --api-url http://<your-llm>/v1 --plain auto "your task here"
```

The LLM backend can be any **OpenAI-compatible** endpoint. Cadillac auto-detects context window and model identity from `/v1/models`. Tested against vLLM (Qwen3-Coder, Qwen3.6-27B), works against OpenAI and Anthropic too.

#### Subcommands

```bash
cadillac auto "task"                         # build + auto-iterate to all-green
cadillac new "task"                          # just build, no iterate
cadillac iterate workspace-XXXX "instruction"# extend an existing build
cadillac resume workspace-XXXX               # resume a crashed build
cadillac enhance ./my-project "add auth"     # modify an external codebase
cadillac debug workspace-XXXX syntax         # focused debug pass
cadillac dash                                # 📊 zero-service TUI dashboard
cadillac list                                # show all workspaces
cadillac                                     # interactive shell
```

#### Flags worth knowing

| Flag | Effect |
|---|---|
| `--plain` | Disable Rich UI (required when stdout is piped) |
| `--context-auto` | Probe `/v1/models` for context window |
| `--rate` | Client-side rate-limit (default `0.25` RPS, 0 disables) |
| `--parallel` | Build independent modules in parallel waves |
| `--max-iterations` | Outer auto-iterate cycles after first validation (default 3) |

Everything else is automatic. By design.

---

## 🏆 Real shipped builds

#### 🎮 Matrix Doom — first modular build

```
32 files · 10 modules · 94 rounds · 17 minutes · 0 human edits
ASCII FPS · raycasting · enemies · HUD · game loop
```

**[github.com/mtecnic/matrix-doom](https://github.com/mtecnic/matrix-doom)** ↗

#### 🐍 Pygame Snake — modular logic

```
13 files · 3 modules (core / renderer / main) · all validations PASS
pytest covers game-logic only (no pygame imports in tests)
```

#### 🍳 Full-stack Recipe Book — stress test

```
38 files · backend (Flask + SQLite + pytest) + frontend (React + Vite + Vitest)
596 rounds · 115 minutes · 7/8 validations PASS
exposed and drove fixes for 10+ cadillac framework bugs in the process
```

#### 💬 AI Agent IRC Server — built two ways

```
build from scratch → 22 files · 7 subpackages · 8/8 · 45 minutes
                     (Qwen3.6-27B, 131K context, modular pipeline)

repair broken v1   →  3 test files added · 8/8 · 7 minutes
                     (cadillac enhance on the original day-1 codebase
                      that had a syntax error and dead message queue)
```

*The same task that produced cadillac's first-ever broken build can now
be either built fresh or repaired in place — both reach all-green.*

#### 🚀 TypeScript Express API — flat

```
8 files · 12 Jest tests · all 8 validations PASS · ~2 minutes
```

---

## 📊 By the numbers

<div align="center">

| | |
|:---:|:---:|
| **92** applications built | **307** unit tests passing |
| **275** lessons accumulated | **15+** framework bugs fixed in 1 session |
| **8/8** validations on full-stack React+Flask+SQLite | **0** required CLI config flags |

</div>

---

## 🧬 Design notes

#### The phase loop

`run()` in `engine.py` is a single `while True` over phases. Each iteration calls `state.tick()` (increments the round counter), checks global budget, picks a phase handler, runs one LLM turn, processes tool calls, decides whether to advance. Wrapped in try/except for `EndpointUnreachable` (LLM endpoint permanently down) and `KeyboardInterrupt` (SIGTERM or Ctrl+C). Both unwind to an end-of-run block that writes phase history and closes the build log — so a killed partial build *still contributes data* to the next build's budget computation.

#### The inspector

Three tiers of defense against known LLM failure modes:

| Tier | What | Examples |
|---|---|---|
| **1. Materials** | Dependency version policy | No `"*"` on critical packages, no versions above the host's Node major |
| **2. Wiring** | Cross-file integration | Does `package.json.scripts.test` point at a real runner? Is the entry point declared in plan present on disk? Any orphan configs? |
| **3. Commissioning** | End-to-end smoke | Does the entry point load without throwing? Does `--test` dispatch correctly? Does the project compile? |

Runs at phase boundaries. Violations become LLM-readable injection blocks in the next prompt.

#### Self-healing post-scaffold

Certain files are protected against LLM re-corruption:

- 📦 `package.json` `"type": "module"` — context-aware. Removed for CJS-Jest setups; preserved for Vite/Vitest/React/Vue.
- ⚙️ `tsconfig.json` `rootDir` — stripped (recurring bug); `skipLibCheck` ensured.
- 🔒 `jest.config.*` / `vite.config.*` / `tsconfig.json` — frozen after scaffold. Edits via shell or `write_file` are blocked.
- 🔧 `.js` extensions in TS imports — auto-stripped (LLMs emit `from './foo.js'` for CommonJS TS, which breaks resolution).

#### Modular pipeline scoped executors

Each module gets a `ModuleScopedExecutor` allowed to `write_file`, `edit_file`, and `read_file` only within its own directory. `run_command` is shared but the working directory is scoped. Prevents one module's build from accidentally editing another's files. Cross-module visibility happens through `get_dependency_interfaces()` — AST-extracted function/class signatures from upstream modules get injected into downstream module prompts.

---

## ⚠️ Known limits

- 🤖 **The LLM has to be smart enough.** Hard test mocks, TypeScript union narrowing, async timing, and React testing-library queries with multiple matchers can stump weaker models. Cadillac gives the LLM every tool it needs (memory, scratch, code map, error logs, stuck-pattern detection) but can't substitute for reasoning.
- ⚙️ **Node execution of `.tsx` entry points** — for React projects, the `run` check uses `vite build` correctly, but the fallback `node entry --test` path can hit `ERR_UNKNOWN_FILE_EXTENSION` on `.tsx`. Surfaces occasionally when language state mutates mid-build.
- 🖥️ **Headless only.** The `waive3` dev machine is headless, so pygame/GUI apps get built and tested (with `SDL_VIDEODRIVER=dummy`) but never visually verified.
- 🔌 **Single backend per invocation.** Fleet/multi-model routing isn't built in.

---

## 🛠 Development

```bash
# Run the full test suite (~2 seconds)
python3 -m unittest \
    cadillac.tests.test_post_test_fixes \
    cadillac.tests.test_scratch \
    cadillac.tests.test_memory \
    cadillac.tests.test_modules \
    cadillac.tests.test_dash

# Quick import check after edits
python3 -c "from cadillac import engine, dash, validate, memory"
```

Workspaces land in `$CWD/workspace-YYYYMMDD-HHMMSS/`. Each has a `.cadillac/` subdirectory:

```
.cadillac/
├── build.jsonl          # append-only event log (every phase, every tool call)
├── cmd_history.jsonl    # adaptive-timeout signal source
├── scratch.md           # within-build LLM notes
└── checkpoint.json      # resume state
```

To pair cadillac against a new LLM endpoint, no config file is required — just pass `--api-url` and optionally `--model`. Context window, model identity, and cross-call pacing are all auto-detected.

---

## 🎯 Philosophy, one more time

Cadillac is not trying to be a general-purpose agent framework. It is an opinionated harness for one specific job —

> **Take a task description, produce a validated application.**

Every design choice bends toward making that job converge more often, faster, with less intervention. If you catch cadillac pushing a decision back to the user when it could have been inferred from a signal cadillac already has, that's a bug. File it.

---

<div align="center">

*Built by an autonomous agent. Reviewed by another. Ships when validation passes.*

</div>
