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
![Tests](https://img.shields.io/badge/tests-616%20passing-2ea44f?style=flat-square)
![Builds](https://img.shields.io/badge/applications%20built-146-blue?style=flat-square)
![Lessons](https://img.shields.io/badge/lessons%20learned-403-purple?style=flat-square)
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
| **I** | **Iterative** | Every validation failure feeds back as `retreat_to_build`. The state machine doesn't fail — it loops with new context until the 10-check gate goes green or budget runs out. | `phases.PhaseState.retreat_to_build` |
| **L** | **Learning** | `memory.jsonl` accumulates lessons (403 today, tag-filtered, source-task-aware decay, confidence-scored). `phase_budgets.jsonl` records rounds-per-phase so the next build's budget is computed from the previous one's reality. The `improve` cycle audits cadillac's own source against a test matrix, proposes patches, and writes meta-lessons when the matrix score lifts. | `memory.recall` · `memory.record_phase_outcome` · `improve.applier.commit_applied` |
| **L** | **Lifecycle** | Full **SPEC → PLAN → DEPS → SCAFFOLD → REVIEW → BUILD → INTEGRATE → WIRING → VALIDATE → CRITIC → RUNTIME → PACKAGE** pipeline, wrapped in a progressive-tier outer loop. Not "code generation" — *application lifecycle*. The output is a packaged, runnable project verified against real flows, not just unit tests. | `phases.Phase` · `phases.PHASE_ORDER` · `engine.run` |
| **A** | **Adaptive** | Every meaningful value is derived from a signal cadillac already sees. Timeouts from `cmd_history.jsonl`. Context budgets from `/v1/models`. Version pins from `node --version`. Phase budgets from p90 of past rounds. The user never tunes any of this. | `tools._adaptive_timeout` · `engine.compute_context_budget` · `inspector.approved_versions_for_host` · `phases.compute_budgets` |
| **C** | **Compiler** | Task description in. **Validated** application out. Like a compiler, the artifact must pass an uncompromising check before it's emitted. Like a compiler, the output is deterministic given the same input + memory state + seed. | `validate.run_validation` (the 10-check gate) |

> *Cadillac is a* ***critic-driven, autonomous, decomposing, iterative, learning lifecycle*** *for* ***adaptive compilation*** *of natural-language tasks into validated applications.*

---

## 🔧 How it works

```
        ┌──────┐                                                                       ┌──────────┐
        │ SPEC │ ── must / should / could user stories (LLM expanded from one sentence)│ PACKAGE  │
        └──┬───┘                                                                       └──────────┘
           ▼                                                                                ▲
  ┌─────┐  ┌──────┐  ┌──────────┐  ┌────────┐  ┌───────┐  ┌───────────┐  ┌────────┐  ┌──────────┐  ┌────────┐  ┌─────────┐
  │PLAN │→ │ DEPS │→ │ SCAFFOLD │→ │ REVIEW │→ │ BUILD │→ │ INTEGRATE │→ │ WIRING │→ │ VALIDATE │→ │ CRITIC │→ │ RUNTIME │
  └─────┘  └──────┘  └──────────┘  └────────┘  └───────┘  └───────────┘  └────────┘  └──────────┘  └────────┘  └─────────┘
  plan +   npm/pip   stub files    adversarial  fix        glue           cross-     12-check      complete-   real HTTP
  spec     install                 critic       cycle      phase          layer      pipeline      ness        flows /
  modular?                                                                HTTP                     audit       CLI runs /
                                                                          smoke      tier 1: must            lib usage
                                                                                     tier 2: must+should
                                                                                     (could on --full-spec)
```

**Flat pipeline** — for projects with fewer than 15 files. One agent, one manifest. Everything happens in a single phase loop.

**Modular pipeline** — for 15+ files. Plan is decomposed into modules, sorted by dependency, built in waves. Each module gets its own scoped executor (can only write to its directory), its own scratch file, its own code-map. Cross-module visibility happens through AST-extracted interfaces from already-built upstream modules.

**Progressive tiers** — for any build with a non-trivial spec. Tier 1 builds the must-stories and must go green through VALIDATE + CRITIC + RUNTIME before tier 2 layers should-stories onto the green base. A long build that fails on a hard should-story still ships the must tier. `--full-spec` adds tier 3 (could-priority).

All pipelines share the same phase state machine, adaptive budgets, memory, validation gate, completeness audit, and runtime probe.

---

## ✅ The 12-check validation gate

```
┌──────────────┬──────────────────────────────────────────────────────────────────┐
│  syntax      │  py_compile / tsc --noEmit / node --check on every source file  │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  imports     │  every import resolves to stdlib, declared dep, or local module │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  static_names│  pyflakes scans for undefined-name bugs that NameError at       │
│              │  runtime — catches "InputPoller used but not imported" pre-run  │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  lint        │  ruff / eslint with auto-configured safe rule set                │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  security    │  static OWASP-class scan: sql_fstring, shell_true_with_interp,  │
│              │  hardcoded_secret, weak_crypto, tls_verify_false, etc. with     │
│              │  context-aware false-positive guards (PRAGMA / `{placeholders}` │
│              │  / `{set_clauses}` parameterized idioms are NOT flagged)        │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  operational │  deploy-readiness probes — schema_integrity (NOT NULL columns   │
│              │  vs literal None at INSERT sites), missing_env (boots backend   │
│              │  with required env stripped, expects fail-fast naming the var), │
│              │  sigterm_responsiveness (5s clean shutdown after SIGTERM)       │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  framework   │  Flask routes registered? React app.mount? type:module set?    │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  functional  │  AST-parse __init__.py re-exports; verify each name resolves    │
│              │  to a real symbol in the package                                 │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  run         │  entry point executes a smoke-test invocation cleanly           │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  smoke_run   │  for curses/pygame apps: monkey-patches a fake screen, runs    │
│              │  the REAL no-args entry path 60 frames — catches broken main() │
│              │  paths that --test mode bypasses                                 │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  tests       │  pytest / vitest / jest all green                               │
├──────────────┼──────────────────────────────────────────────────────────────────┤
│  naming      │  plan's declared module names match the filesystem              │
└──────────────┴──────────────────────────────────────────────────────────────────┘
```

Failing a check triggers retreat-to-BUILD, up to `max_validate_retries` cycles. Same fingerprint repeated 3× triggers **surgical mode** (see Resilience layers). When all 12 pass, the build proceeds to CRITIC, then RUNTIME, then PACKAGE.

The newest checks address recurring deploy-time failure classes:

- `operational` — caught PingFlux's `status_code INTEGER NOT NULL` receiving `None` from a network-failure path; flags backends that silently accept missing required env vars (operators learn the bug from a crashloop instead of a clear error)
- `security` — context-aware false-positive guards mean the parameterized-fragment idiom `f"WHERE x IN ({placeholders})"` and SQLite's non-parameterizable `f"PRAGMA {name}={value}"` are no longer flagged as SQL injection
- `functional` — was line-based regex on `__init__.py` (silently mis-reading multi-line `from .x import (...)` as exporting `(` as a name); now ast-parses

---

## 🛡 Resilience layers

Five mechanisms sit between BUILD and PACKAGE so a build that almost made it doesn't die at the finish line.

#### 📝 SPEC — explicit user stories from a terse task

Before PLAN, an LLM call expands one-sentence tasks into 15-40 structured `Story` objects with `priority ∈ {must, should, could}`, `acceptance` criteria, and `category`. Stored to `<workspace>/spec.json`. Architecture, manifest, and BUILD prompts all see the spec — so the build has an explicit target, not the LLM's improvised interpretation.

> *"Build a habit tracker"* expanded to 25 stories on a recent run — including password reset, input sanitization, future-date validation, and graceful HTTP error codes. Things a typical one-shot build silently skips.

Source: `cadillac/spec.py` · `cadillac/critic.py`

#### 🔍 CRITIC — completeness audit after VALIDATE green

Two-stage: a static keyword prefilter flags stories whose terms never appear in the workspace; for ambiguous remainder, an LLM second-opinion compares the spec against the codemap + manifest. Actionable gaps (must / should priority) bounce to BUILD for a completion pass. Cap: one critic-driven retreat per build.

Real example from a build last week: VALIDATE green on all checks; CRITIC scored `0.86` and flagged S03 (`User logs out`), S10 (`Delete habit`), S18 (`Profile retrieval`) as missing. The LLM added them on the bounce-back pass.

#### 🌐 RUNTIME — drive real flows against the live artifact

After CRITIC clean, runtime verification picks a strategy by language family + project shape:

| Strategy | When | Probe shape |
|---|---|---|
| **http** | Flask/FastAPI/Express detected | Chained HTTP flows: signup → grab token → call protected route, with capture + status + body subset assertions |
| **cli** | Runnable binary, no HTTP listener | Scripted `argv` + `stdin` runs with exit-code and stdout substring assertions |
| **library** | Public API surface, no entry runner | Short runnable usage snippets that import from the package and assert on return values |
| **skip** | Static site / WordPress / browser extension / interactive | No surface to drive |

The HTTP runner reuses validate.py's WIRING boot infrastructure (process group, listening wait, group-kill teardown). Catches the bugs unit tests and code review can't see — "logout returns 200 OK but the token still works", "mark-done returns 200 but `last_completed` stays null".

Source: `cadillac/runtime/__init__.py` (orchestrator) · `cadillac/runtime/{http,cli,library}_runner.py` · `cadillac/runtime/types.py`

#### 🩺 Surgical mode — stuck-loop detection + targeted fix

Validation failures get fingerprinted by `check_name:file:line:error_class`. Same fingerprint across 3 consecutive retries triggers a focused single-file edit pass with ~500 tokens of context (not the usual 30K). For `undefined name` errors specifically, the prompt is augmented with the file's existing imports, a workspace grep for `class X`/`def X`/`X = ...` candidates, and sibling `__init__.py` exports — so the LLM can pick the right import to add instead of renaming to another undefined symbol. Cap: one surgical attempt per fingerprint.

> A build that died at retry 5/5 on `undefined name 'clean_email'` would today be unstuck in retry 3 by surgical mode, with a 1-line edit.

Source: `cadillac/surgical.py`

#### 🪜 Progressive tiers — must → should → could

Instead of building all 25 stories in one shot, the engine slices into priority tiers. Tier 1 (must) builds first; VALIDATE + CRITIC + RUNTIME must all clear before tier 2 (must+should) layers should-stories onto the green base. Tier 3 (could) only runs with `--full-spec`. Per-tier resets of CRITIC/RUNTIME/surgical flags so each tier re-evaluates its surface.

A 30-story build that fails on a hard should-story still ships the must tier. The catastrophe footprint of one bad story shrinks to one tier instead of one build.

Source: `cadillac/phases.py:PhaseState.{current_tier,validate_retries,...}` · `cadillac/engine.py:run()` outer tier loop

---

## 🖥️ Terminal UI

Cadillac is a **terminal-only** tool. Two views, both pure-stdlib + Rich, no web service, no daemon, no port.

#### Live build display

Runs by default during `auto` / `new` / `iterate` / `resume` / `enhance`. File tree on the left, activity log on the right, phase bar + validation mix at the top — all updating in real time:

```
╭── Cadillac · workspace-20260526-114502 ──────────────────────────────────────╮
│ PLAN ✓  DEPS ✓  SCAFFOLD ✓  REVIEW ✓  BUILD ⟲  INTEGRATE _  VALIDATE _      │
│ Round 47/120 · 12 files · API ~14k in / 16k out · 12m 04s                    │
├──────────────────────────┬───────────────────────────────────────────────────┤
│ ▼ workspace/             │ [BUILD] Round 47/120                              │
│   ▶ api/                 │   read_file(services/auth_service.py)             │
│   ▼ services/            │   << 87 lines                                     │
│     ● auth_service.py    │   edit_file(services/auth_service.py)             │
│     ○ habit_service.py   │     anchor: "def change_password"                 │
│   ▶ persistence/         │     replace: 4 lines                              │
│   ▶ tests/               │     ✓ applied                                     │
│   ○ main.py              │   run_command(python3 -m pytest -q)               │
│   ○ requirements.txt     │     << 88 passed in 10s                           │
│ ─────────────────        │ [Validation: nam✓ imp✓ syn✓ lin✓ sec✓ ope✓        │
│ Memory: 10 lessons       │              fra✓ fun✓ run✓ smk✓ tst✓]            │
│ Tags: python, flask      │                                                   │
╰──────────────────────────┴───────────────────────────────────────────────────╯
```

The file tree marks new files (`○`), recently-edited files (`●`), and currently-being-edited files (`▶`). The activity log streams every tool call (`>> tool(args)`) and its result (`<<`). Phase transitions, validation results, CRITIC scores, RUNTIME flow outcomes, and STUCK / SURGICAL events all surface here as they happen.

Pass `--plain` to disable Rich and fall back to line-by-line stdout — required when piping output to a file or running in CI / non-TTY environments.

Source: `cadillac/display.py` · `cadillac/events.py`

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
| `m` | **Memory** | 403 accumulated lessons, top by confidence, tag histogram, do/don't split, cross-task decay annotations |
| `b` | **Budgets** | Phase-history stats: mean / p90 / max rounds per (tag-bucket, phase) |
| `e` | **Events** | Live tail of selected workspace's `.cadillac/build.jsonl` |
| `?` | **Help** | Key reference |

**Zero services.** No port. No daemon. Pure read-only filesystem access. When you press `q`, nothing remains. No web UI exists or is planned — the design intent is that the only state outside your shell session is the JSONL files on disk, and any tooling that wants to read them can do so directly.

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
  "used": 7,
  "source_task": "Build a React + Vite SPA with vitest tests"
}
```

Before each build, `recall(task)` scores them by **tag-filter + keyword-overlap + recency + confidence + cross-task decay** and injects the top 10 into the system prompt. Lessons whose triggers appear in fresh errors *lose* confidence (`penalize_backfired`); lessons that survive an applied build *gain* it.

The 2026-05-26 hygiene pass added two precision controls:

- **`source_task` decay**: lessons whose originating task has zero stack-tag overlap with the current task get a 5× score penalty. Stops top-used IRC-server lessons (formerly used 87×) from dominating recall on unrelated builds.
- **Content tag inference**: `infer_tags_from_text` expands library mentions to canonical stack tags (`aiosqlite` → `{python, sqlite, asyncio}`). New lessons can't land untagged anymore (parse_reflection backfills from body text when the caller omits).
- **Fresh-lesson floor**: new reflection lessons start at confidence 0.3 (was 0.5). Requires 2+ successful reinforcements before outweighing the scoring noise floor.
- **Meta-lessons**: when `cadillac improve` lands a patch and the matrix score lifts ≥0.05, the applier writes an `architecture` lesson tagged `[cadillac, self]` capturing what worked.

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
├── 🎛  engine.py          ~3500 lines — phase state machine, chat loop, modular orchestration,
│                                         tier loop, stuck-loop detection, CRITIC + RUNTIME wiring
├── 🧱  modules.py         ModuleSpec, ModularPlan, dependency topo-sort, cycle detection
├── 📝  prompts.py         LLM templates for each phase (flat + modular variants)
├── 🔨  tools.py           ToolExecutor + ModuleScopedExecutor, sandboxed I/O
├── ✅  validate.py        The 12-check pipeline + WIRING dynamic probe + contract alignment
├── 🩺  operational.py     Deploy-readiness gates — schema integrity / missing-env / SIGTERM
├── 📝  spec.py            Story + Spec dataclasses, generate_spec(), Spec.subset() for tiers
├── 🔍  critic.py          Completeness audit: static prefilter + LLM second opinion
├── 🌐  runtime/           Runtime verification package
│      ├── __init__.py    Orchestrator + strategy dispatch
│      ├── types.py       Probe / ProbeFailure / VerificationResult
│      ├── format.py      Shared `format_for_iterate(failures)` builder
│      ├── http_runner.py Flow / FlowStep, generate from spec+contract, drive live backend
│      ├── cli_runner.py  ScriptedRun, generate from spec+entry, scripted subprocess
│      └── library_runner.py UsageExample, generate snippets for Python/Node/Rust libs
├── 🪜  surgical.py        Stuck-loop targeted fix — name resolution hints, edit + re-check
├── 📜  contracts.py       Contract + Endpoint, the artifact both sides import from
├── 🎯  phases.py          Phase enum, budgets, PhaseState (tier + stuck + critic + runtime flags)
├── 📋  manifest.py        Thread-safe file registry with structural summaries
├── 🔍  inspector.py       3-tier building-code enforcement (materials/wiring/commissioning)
├── 🗺️  codemap.py         Tiered source representation (AST tier 1 → regex tier 2 → raw tier 3)
├── 🧠  memory.py          Lessons + tag-aware recall + cross-task decay + meta-lessons
├── 📓  scratch.py         Per-module within-build scratch files
├── 🌐  languages.py       Python + TypeScript + Go + Rust + WordPress + browser-ext + PyTorch
├── 📐  quality.py         Coding standards, anti-patterns, few-shot examples per stack
├── 🛡  adversarial.py     Second-LLM-pass test generation, objective-driven probes
├── 🖥️  display.py         Rich live terminal UI (file tree + activity log + validation)
├── 📡  events.py          Structured events decoupling engine from display
├── 📈  progress.py        progress.md writer + compact LLM context
├── 📊  dash.py            Zero-service TUI dashboard (this README ↑)
├── 💻  cli.py             Interactive shell, workspace resolution
├── ⚙️   cadillac.py       CLI entry point (argparse) — --full-spec, --parallel, etc.
├── 🔁  improve/           Self-improvement cycle — audit / probe / correlate / propose / apply
├── 💾  memory.jsonl       403 accumulated lessons (tag-filtered, source-task-aware)
├── 📚  phase_budgets.jsonl   Cross-build phase-round history
└── 🧪  tests/             616 unit tests covering all of the above
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
cadillac improve                             # self-improvement cycle vs. test matrix
cadillac dash                                # 📊 zero-service TUI dashboard
cadillac list                                # show all workspaces
cadillac                                     # ↓ interactive shell ↓
```

#### Interactive shell

Running `python3 -m cadillac` with no subcommand drops into a REPL — handy for chaining `new → list → open → iterate` against the same endpoint without re-typing `--api-url` each time.

```
$ cd /home/waive3/sandbox
$ python3 -m cadillac --api-url http://192.168.86.39:8000/v1
╭─────────────────────────────────────────╮
│ Cadillac — Autonomous Agent Builder     │
│ API: http://192.168.86.39:8000/v1       │
│ Type help for commands                  │
╰─────────────────────────────────────────╯
cadillac>
```

Shell commands (no `cadillac` prefix needed inside the prompt):

| Command | What |
|---|---|
| `new <task>` | Start a new build |
| `resume <workspace>` | Resume a crashed/interrupted build |
| `list` | List all workspaces with status summaries |
| `open <workspace>` | Show workspace details (files, phase, validation mix) |
| `iterate <workspace> [msg]` | Re-run BUILD→VALIDATE on an existing workspace |
| `debug <workspace> [target]` | Focused debug pass on a specific failure |
| `lessons` | Show accumulated lessons (top by confidence) |
| `config [key] [value]` | Inspect or override session config |
| `help` | Print this command list |
| `quit` / `exit` / `q` / `Ctrl-D` | Exit the shell |

Workspace names support fuzzy prefix matching — `iterate 20260526` resolves to the most-recent workspace whose name starts with `workspace-20260526`. A typical session:

```
cadillac> new Flask URL shortener with JWT auth
[PLAN] ... [BUILD] ... [VALIDATE] All validations passed!

cadillac> list
  workspace-20260526-114502  ✓ DONE  17/17
  workspace-20260526-101234  ● LIVE  4/12
  ...

cadillac> iterate 20260526-1145 add rate limiting on /shorten

cadillac> lessons
  [do]   When: SQLite write during async handler  → Use aiosqlite ...
  [dont] NEVER: f-string-built SQL with user input ...

cadillac> quit
Bye!
```

#### Flags worth knowing

| Flag | Effect |
|---|---|
| `--plain` | Disable Rich UI (required when stdout is piped) |
| `--context-auto` | Probe `/v1/models` for context window |
| `--rate` | Client-side rate-limit (default `0.25` RPS, 0 disables) |
| `--parallel` | Build independent modules in parallel waves |
| `--max-iterations` | Outer auto-iterate cycles after first validation (default 3) |
| `--full-spec` | Include tier 3 (could-priority stories). Default: must + should only, to bound build wall time. |

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

#### 🎯 Flask Habit Tracker — first full-resilience-stack build

```
17 files · 290 rounds · 92 minutes · status: COMPLETE
2-tier progressive: tier 1 (must, 12 stories) → tier 2 (must+should, 22 stories)
CRITIC + RUNTIME each fired twice (once per tier)
RUNTIME http-strategy: 16 + 20 real flows generated and exercised
Found and reported behavioral gaps that unit tests + adversarial + WIRING all missed:
  - logout returned 200 but the token kept working
  - mark-done returned 200 but `last_completed` stayed null
```

---

## 📊 By the numbers

<div align="center">

| | |
|:---:|:---:|
| **146** applications built | **616** unit tests passing |
| **403** lessons accumulated | **15+** framework bugs fixed in 1 session |
| **12/12** validation checks + SPEC + CRITIC + RUNTIME | **0** required CLI config flags |
| **2-tier progressive build** (must / must+should) | **1×** surgical mode per stuck fingerprint |

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
# Run the full test suite (~14 seconds, 616 tests)
python3 -m unittest discover -s cadillac/tests -q

# Run the resilience-layer tests specifically
python3 -m unittest \
    cadillac.tests.test_spec_critic \
    cadillac.tests.test_runtime_dispatch \
    cadillac.tests.test_runtime_http \
    cadillac.tests.test_runtime_cli \
    cadillac.tests.test_runtime_library \
    cadillac.tests.test_operational \
    cadillac.tests.test_surgical \
    cadillac.tests.test_progressive_tiers

# Quick import check after edits
python3 -c "from cadillac import engine, dash, validate, memory, spec, critic, surgical, operational; from cadillac.runtime import runtime_verify"
```

Workspaces land in `$CWD/workspace-YYYYMMDD-HHMMSS/`. Each has both a top-level `spec.json` and a `.cadillac/` subdirectory:

```
workspace-YYYYMMDD-HHMMSS/
├── spec.json                  # SPEC output — user stories the build targets
├── contracts.json             # API contract for full-stack builds
├── architecture.md            # PLAN output
├── plan.json                  # manifest of files to write
├── progress.md                # human-readable phase trace
└── .cadillac/
    ├── build.jsonl            # append-only event log (every phase, every tool call)
    ├── cmd_history.jsonl      # adaptive-timeout signal source
    ├── scratch.md             # workspace-level within-build LLM notes
    ├── checkpoint.json        # resume state
    ├── flows.json             # runtime/http: generated Flow objects
    ├── cli_runs.json          # runtime/cli: generated ScriptedRun objects
    ├── usage_examples.json    # runtime/library: generated UsageExample snippets
    ├── adversarial/           # adversarial probe tests
    └── modules/               # per-module checkpoints (modular builds)
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
