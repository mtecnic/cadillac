# Launch playbook — Show HN

Working notes for the launch. Not for the README; check this in but it's
ours, not the audience's. Delete (or move to `.github/`) once we've shipped.

---

## Pre-flight checklist

- [x] LICENSE (Apache 2.0) committed
- [x] README hero rewrites the wedge in 5 bullets
- [x] No host-identifying info anywhere in repo
- [x] 616 unit tests green
- [ ] **Record demo** — 60-90 seconds, plan below
- [ ] **Pin demo as an asciicast or short MP4 in the README**, just under the
      transcript block (before "What's different")
- [ ] Make repo public (GitHub repo settings → Change visibility)
- [ ] Post on HN at a strategic time
- [ ] Watch the post for the first 2 hours and reply to every substantive
      comment — engagement in hour 1 is the dominant ranking signal

---

## Title options (pick one — first is the lead)

1. **Show HN: Cadillac – a coding agent that runs the app and tests real user flows before shipping**

   Why: leads with the differentiator. Most viewers skim titles.

2. Show HN: Cadillac – sentence-to-app builder with operational gates and runtime verification

3. Show HN: Cadillac – an autonomous coding agent that finishes when it actually works

Tradeoff: #1 is most specific and easiest to skim; #3 is the most marketing-friendly
("actually works" is provocative). I'd lead with #1 unless you want the bait.

---

## First-comment post text

```
Cadillac is a harness I've been building that takes a one-sentence task
("Flask habit tracker: users sign up, log habits, view streaks, SQLite
storage") and ships a packaged, validated, *behaviorally-tested*
application — unattended — against any OpenAI-compatible LLM endpoint
(tested with vLLM/Qwen3-Coder, also runs against OpenAI/Anthropic APIs).

I built it because every other coding agent I tried stopped at "the code
compiles and the unit tests pass." That's where the interesting bugs
start. The Flask habit-tracker build that motivated most of the recent
work shipped a clean app where `POST /auth/logout` returned 200 OK — but
the token kept working on the next request. Unit tests passed. The code
existed. The behavior was wrong. Nothing in the pipeline caught it.

So Cadillac now has five layers between BUILD and PACKAGE that other
agents I've used don't:

- Operational gates: boots the backend with a required env var stripped
  and expects fail-fast; sends SIGTERM and expects clean 5s shutdown;
  AST-scans for `INSERT INTO ... VALUES (?, None)` against NOT NULL columns.
- A completeness CRITIC: an LLM second-opinion that compares the running
  code against an explicit user-story spec (extracted from the prompt
  by an earlier SPEC phase) and bounces missing features back to BUILD.
- Runtime flow verification: boots the artifact and drives real flows.
  HTTP for backends (chained request flows with capture + assertions),
  scripted argv for CLIs, runnable snippets for libraries. This is what
  caught the logout bug.
- Surgical mode for stuck loops: when the same validation error repeats
  3 retries in a row, the harness switches from generic BUILD-iterate to
  a focused single-file edit with ~500 tokens of context (vs the usual
  30K). For "undefined name" errors specifically, the prompt is augmented
  with workspace-wide grep for the missing symbol's definitions.
- Progressive tiers: long specs build in waves — must-stories must go
  green before should-stories layer on top. A big build that fails on a
  hard should-story still ships the must tier.

Stack: pure Python 3.12+, no LLM client libraries (uses the OpenAI HTTP
spec directly), Rich for terminal UI. 616 unit tests, 146 builds shipped
so far. Apache 2.0.

Things I'd love feedback on:
- The runtime verification dispatch (http / cli / library) — is the
  cli probe shape useful or noise?
- Whether the SPEC → CRITIC loop catches real "we forgot to build it"
  failures in your own attempts (I've seen 0.86 / 0.81 completeness
  scores on the same task across runs)
- Where the surgical-mode hint augmentation falls short — today it
  handles `undefined name` well; what other error classes should get
  similar treatment

Repo: https://github.com/mtecnic/cadillac
Demo (90s asciicast): <add URL after recording>

Happy to answer anything.
```

Length is intentional — HN rewards depth in the first comment, and the
algorithmic skim-rate is high enough that bullet structure dominates over
prose. ~400 words is in the sweet spot.

---

## Demo storyboard (90 seconds target)

The Flask habit-tracker build is the right demo because:
- Spec is non-trivial (25 stories)
- All five resilience layers fire visibly
- Output is a real, demoable Flask app at the end

Real build wall time was ~92 minutes. We want a 90-second cut. **Heavy
speed-up on the slow parts, normal speed for the moments that matter.**

#### Recording approach

Use `asciinema` for the terminal recording, then either:
- (a) Embed the asciicast directly in the README via `asciinema upload`
  → asciicast URL in `<a><img src="..."></a>` tag.
- (b) Convert to MP4 via `agg` (asciinema's GIF/video converter) for HN +
  Twitter compatibility.

I'd do BOTH — asciicast is the preferred format in dev circles (re-playable,
copy-paste from frame), MP4 is what 90% of HN visitors will actually click.

#### Scene-by-scene timing

| Time | Speed | What's on screen |
|---|---|---|
| 0:00–0:05 | 1x | Title card: `cadillac auto "Flask habit tracker: users sign up, log out, create habits, view streaks, search by name. SQLite."` |
| 0:05–0:10 | 1x | `[SPEC] expanding task into user stories...` → `[SPEC] 25 stories (13 must)` |
| 0:10–0:15 | 1x | `[TIER] starting tier 1/2: must (13 stories)` |
| 0:15–0:30 | 8x | Module scaffolding burst: `config / models / repository / services / api`, each "Build complete!" landing |
| 0:30–0:40 | 8x | First VALIDATE retry, `[FAIL] static_names: undefined name 'User'` |
| 0:40–0:50 | 1x | **HERO MOMENT**: `[STUCK] static_names:auth/service.py:158 — entering surgical mode` → `[SURGICAL/CLEARED]` (this is the differentiator nobody else has) |
| 0:50–0:55 | 4x | Validate retry → `[All validations passed!]` |
| 0:55–1:05 | 1x | **HERO MOMENT 2**: `[CRITIC] LLM reviewing 15 ambiguous stories...` → `[CRITIC] completeness score = 0.86; 3 actionable gaps; bouncing to BUILD` |
| 1:05–1:15 | 4x | Build pass to fill gaps → green again |
| 1:15–1:25 | 1x | **HERO MOMENT 3**: `[RUNTIME] strategy=http (http backend detected)` → `[RUNTIME] 16 flow(s) generated` → first flow firing, then `[RUNTIME] strategy=http probes_run=16 failures=13` |
| 1:25–1:30 | 1x | `[TIER] advancing to tier 2/2: must+should (22 stories)` → fade to `COMPLETE | 290 rounds | 17 files` |

3 hero moments, ~25s each at normal speed, 65s of speed-up in between. Total
budget: 90s with the title card.

#### Optional cold-open (alternative format)

If the recording feels dense, open with a 5-second freeze frame showing
*just* the cURL output:

```
$ curl -X POST localhost:5005/auth/logout -H "Authorization: Bearer $T"
{"message": "Logged out successfully"}

$ curl localhost:5005/habits/ -H "Authorization: Bearer $T"
[{"name": "Run", ...}]      ← still works. token wasn't actually invalidated.
```

Then cut to: "Cadillac runs this kind of test against every backend it
builds." Then the 90s build cut.

The cold open gives non-technical viewers a concrete reason to care
about runtime verification before the build cut shows it firing.

---

## Posting time

HN front-page math: post when the most US-tech audience is reading.

- 8-10 AM PT, weekday: best for tech-tooling posts. Devs check HN with
  morning coffee.
- 1-2 PM PT, weekday: secondary slot.
- Avoid: Fri after 11am PT (weekend drift), late Sun (Monday is a fresh
  front page).

Pick **Tuesday or Wednesday 9 AM PT** unless something specific is happening.

---

## First-hour tactics

The first 60-120 minutes of votes on a Show HN post determine whether it
hits the front page. Be available:

- Reply to every substantive top-level comment within 10 minutes for
  the first 2 hours
- Don't reply with one-liners. Engage. People upvote authors who treat
  the discussion as a real conversation.
- If a comment hits hard ("this is just Devin/OpenHands/etc"), don't
  defend — concede what's true and explain the specific design choices
  that diverge. Honest > defensive.
- If a bug report comes in, file it immediately as a GitHub issue and
  link the issue in your reply.

Don't game votes (don't ask friends to upvote — HN detects this and
shadow-bans).

---

## Promotion path after HN

In rough order of leverage:

1. **/r/LocalLLaMA** (or /r/MachineLearning depending on tone): your
   key wedge for this audience is "runs locally against vLLM/Qwen, not
   another API-dependent agent". Lead with that.
2. **Twitter/X**: thread of 5-7 posts, one per resilience layer, each
   with a short clip from the demo recording. Tag accounts that follow
   the agent-tooling space (don't @-mention people you don't know).
3. **lobste.rs**: smaller audience but tech-quality. Same post angle as HN.
4. **Hacker Newsletter / TLDR.tech**: if it goes well on HN, both will
   index it automatically.
5. **A blog post** on the resilience patterns specifically — this is the
   durable artifact. HN is a 24-hour spike; the blog post is what
   recruiters and curious devs find via Google three months later.

---

## What success looks like

- 200+ stars in 48 hours = scenario B mid (worth it)
- 1K+ stars = scenario B front-page-top (real signal)
- 3+ substantive issues from people who actually tried it = community
  forming
- 1 attempt to fork-and-extend = something real

What success doesn't look like:
- "Cool!" comments without action
- 1000 stars but 0 issues (the discovery is shallow, nobody actually
  ran it)
