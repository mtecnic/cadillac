"""Persistent lesson memory — learns from build mistakes across sessions."""

import json
import os
import re
import time
from dataclasses import dataclass, asdict, field

MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_PATH = os.path.join(MEMORY_DIR, "memory.jsonl")
# Per-phase rounds-used history. Cross-build; consulted by `compute_budgets`
# so future builds budget from what ACTUAL past builds needed, not from a
# static table. Each line: {"ts", "phase", "rounds", "n_files", "tags"}.
PHASE_HISTORY_PATH = os.path.join(MEMORY_DIR, "phase_budgets.jsonl")

# Common keywords used to auto-tag lessons by language/framework/phase
_KNOWN_TAGS = {
    "python", "typescript", "javascript", "react", "vue", "angular",
    "electron", "desktop", "go", "golang", "rust", "cargo", "tokio",
    "wordpress", "plugin", "php", "browser", "extension", "chrome", "mv3",
    "pytorch", "torch", "cuda", "gpu", "training", "deep", "transformer",
    "html", "css", "node", "flask", "django", "fastapi", "express",
    "pygame", "click", "asyncio", "aiosqlite", "sqlite", "pytest",
    "vitest", "jest", "phpunit", "modular", "flat",
    "plan", "deps", "scaffold", "review", "build", "integrate", "wiring", "validate", "package",
}


@dataclass
class Lesson:
    ts: float
    type: str           # error_pattern, architecture, tool_pattern, dependency, performance
    trigger: str        # What situation triggers this lesson
    fix: str            # What to do about it
    confidence: float = 0.5
    used: int = 0
    polarity: str = "do"  # "do" = positive lesson, "dont" = anti-pattern
    tags: list = field(default_factory=list)  # Optional: language/framework/phase tags
    # Source task that originated this lesson — used by recall() to deweight
    # lessons whose source has zero tag overlap with the current task. Empty
    # string for legacy lessons recorded before this field existed; those are
    # treated as "stack-agnostic" (no decay applied).
    source_task: str = ""


def load_lessons() -> list[Lesson]:
    if not os.path.exists(MEMORY_PATH):
        return []
    lessons = []
    with open(MEMORY_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lessons.append(Lesson(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
    return lessons


def save_lesson(lesson: Lesson):
    """Append one lesson to memory.jsonl. Lock-protected to serialize
    concurrent appends from parallel module builds — without the lock,
    two threads' writes can interleave and produce malformed JSON lines
    that load_lessons() silently skips, losing data. (Audit H2.)

    Redacted before persistence: lessons are distilled from real failure text
    (tracebacks, shell stderr, config dumps) and outlive the build, so one
    leaked credential would be re-injected into every future build's prompt.
    """
    from ._atomic import atomic_append_lines
    from .redact import redact_obj
    atomic_append_lines(MEMORY_PATH, [json.dumps(redact_obj(asdict(lesson)))])


def save_all(lessons: list[Lesson]):
    """Rewrite the full memory file (used after pruning or confidence updates).

    Atomic rename — a crash between truncate and the final flush would have
    wiped every lesson the system had ever recorded. (Audit H3.) Locked
    against concurrent save_lesson() callers so the rewrite is serialized
    against appends.
    """
    from ._atomic import atomic_write_text, file_lock
    payload = "".join(json.dumps(asdict(lesson)) + "\n" for lesson in lessons)
    with file_lock(MEMORY_PATH):
        atomic_write_text(MEMORY_PATH, payload)


def _word_overlap(a: str, b: str) -> float:
    """Compute Jaccard similarity between word sets of two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def deduplicate(lessons: list[Lesson]) -> list[Lesson]:
    """Merge lessons with same type and >=70% trigger word overlap.

    O(n) hash-bucket implementation: lessons sharing any significant trigger
    word (and the same type) are bucketed together, then merged within each
    bucket using the original word-overlap rule. Backward compatible with the
    previous O(n^2) impl on the existing 82-lesson fixture.
    """
    # Bucket by (type, sorted significant token tuple) for fast grouping.
    # We use a representative token bucket: any token from the trigger that has
    # length >=4 puts the lesson into that token's bucket. Lessons sharing a
    # bucket are then compared with the proper word-overlap rule. This avoids
    # O(n^2) scan while preserving the >=70% overlap merge semantics.
    buckets: dict[tuple[str, str], list[int]] = {}
    for i, lesson in enumerate(lessons):
        tokens = [t for t in lesson.trigger.lower().split() if len(t) >= 4]
        if not tokens:
            tokens = lesson.trigger.lower().split() or ["_empty_"]
        for tok in set(tokens):
            buckets.setdefault((lesson.type, tok), []).append(i)

    # Walk lessons in order; for each, scan only candidates sharing a bucket.
    merged: list[Lesson] = []
    merged_index_for: dict[int, int] = {}  # original index -> merged list index
    for i, lesson in enumerate(lessons):
        candidate_indices: set[int] = set()
        tokens = [t for t in lesson.trigger.lower().split() if len(t) >= 4]
        if not tokens:
            tokens = lesson.trigger.lower().split() or ["_empty_"]
        for tok in set(tokens):
            for j in buckets.get((lesson.type, tok), []):
                if j < i:
                    candidate_indices.add(j)
        absorbed = False
        for j in candidate_indices:
            mj = merged_index_for.get(j)
            if mj is None:
                continue
            existing = merged[mj]
            if _word_overlap(lesson.trigger, existing.trigger) > 0.7:
                if lesson.confidence > existing.confidence:
                    merged[mj] = lesson
                merged_index_for[i] = mj
                absorbed = True
                break
        if not absorbed:
            merged_index_for[i] = len(merged)
            merged.append(lesson)
    return merged


def decay(lessons: list[Lesson], rate: float = 0.05):
    """Decrease confidence of lessons not used in the last 7 days."""
    cutoff = time.time() - (7 * 86400)
    for lesson in lessons:
        if lesson.ts < cutoff and lesson.used == 0:
            lesson.confidence = max(0.1, lesson.confidence - rate)


def infer_task_tags(task_description: str) -> set[str]:
    """Extract known language/framework/phase tags from a task description."""
    words = set(re.findall(r"[a-z0-9]+", task_description.lower()))
    return words & _KNOWN_TAGS


# Synonyms — keywords that map to one of the canonical _KNOWN_TAGS so the
# tag-inference pass catches semantically-equivalent mentions ("aiosqlite"
# is unambiguously a sqlite + asyncio + python signal).
_TAG_SYNONYMS: dict[str, set[str]] = {
    "aiosqlite": {"python", "sqlite", "asyncio"},
    "sqlalchemy": {"python", "sqlite"},
    "fastapi": {"python", "fastapi", "asyncio"},
    "celery": {"python"},
    "redis": set(),
    "tsc": {"typescript"},
    "tsx": {"typescript", "react"},
    "jsx": {"javascript", "react"},
    "npx": {"node"},
    "npm": {"node"},
    "ruff": {"python"},
    "mypy": {"python"},
    "black": {"python"},
    "uvicorn": {"python", "fastapi"},
    "websockets": {"python", "asyncio"},
    "irc": {"python", "asyncio"},
    "tetris": {"python", "pygame"},
    "curses": {"python"},
    "pygame": {"python", "pygame"},
    "rich": {"python"},
    "ascii": set(),
    "wp_query": {"php", "wordpress"},
    "manifest_v3": {"browser", "extension", "chrome", "mv3"},
}


def infer_tags_from_text(text: str) -> set[str]:
    """Like `infer_task_tags` but works on arbitrary lesson trigger/fix text.

    Falls back to synonyms (e.g. `aiosqlite` → `{python, sqlite, asyncio}`) so
    a lesson that only ever mentions a library still picks up the right
    stack tags for cross-task decay in `recall()`.
    """
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    out: set[str] = words & _KNOWN_TAGS
    for syn, mapped in _TAG_SYNONYMS.items():
        if syn in words:
            out |= mapped
    # File-extension hints
    if ".py" in lowered or "pytest" in lowered:
        out.add("python")
    if ".ts" in lowered or ".tsx" in lowered or "tsconfig" in lowered:
        out.add("typescript")
    if ".rs" in lowered or "cargo" in lowered:
        out.add("rust")
    if ".go" in lowered:
        out.add("go")
    return out


def recall(task_description: str, limit: int = 10) -> list[Lesson]:
    """Score and return the most relevant lessons for a task.

    Three filtering layers, applied in order:

      1. **Tag filter (hard)**: a lesson with tags is only considered if its
         tags overlap the task's inferred tags. (Existing behavior.)
      2. **Source-task decay (soft)**: an untagged lesson whose `source_task`
         shares zero inferred-tag overlap with the current task gets a 5×
         score penalty. Stops "irc_server" lessons (originated from one big
         async IRC build, never tagged) from dominating recall on unrelated
         tasks. Lessons with no source_task (legacy, pre-2026-05-26) are
         treated neutrally.
      3. **Content tag inference (soft)**: if the lesson's trigger+fix text
         contains stack keywords (`aiosqlite`, `pytest`, `.rs`...) that don't
         overlap with the current task's tags, apply a 2× penalty.
    """
    lessons = load_lessons()
    if not lessons:
        return []

    # Deduplicate and decay before scoring
    lessons = deduplicate(lessons)
    decay(lessons)

    task_tags = infer_task_tags(task_description)
    if task_tags:
        lessons = [l for l in lessons if not l.tags or set(l.tags) & task_tags]

    task_words = set(task_description.lower().split())
    scored = []
    for lesson in lessons:
        trigger_words = set(lesson.trigger.lower().split())
        fix_words = set(lesson.fix.lower().split())
        all_words = trigger_words | fix_words
        keyword_score = len(task_words & all_words) / max(len(all_words), 1)
        recency_score = 1.0 / (1 + (time.time() - lesson.ts) / 86400)
        total = keyword_score * 2 + recency_score + lesson.confidence + (lesson.used * 0.1)

        # Cross-stack penalty: lesson originated from a task whose stack
        # doesn't intersect the current one. Only meaningful when the
        # source_task field is populated (lessons recorded after the
        # 2026-05-26 schema change). Uses the broader synonym-aware
        # inference so source tasks worded as "async IRC server" still
        # signal {python, asyncio} rather than returning empty.
        if task_tags and lesson.source_task:
            src_tags = infer_tags_from_text(lesson.source_task)
            if src_tags and not (src_tags & task_tags):
                total /= 5.0
        # Content tag inference — soft penalty when the lesson's body
        # text talks about a stack the current task isn't on.
        if task_tags and not lesson.tags and not lesson.source_task:
            content_tags = infer_tags_from_text(
                lesson.trigger + " " + lesson.fix
            )
            if content_tags and not (content_tags & task_tags):
                total /= 2.0

        scored.append((total, lesson))
    scored.sort(key=lambda x: -x[0])
    return [lesson for _, lesson in scored[:limit]]


def format_for_prompt(lessons: list[Lesson]) -> str:
    """Format lessons for injection into system prompt."""
    if not lessons:
        return ""
    do_lessons = [l for l in lessons if l.polarity != "dont"]
    dont_lessons = [l for l in lessons if l.polarity == "dont"]
    lines = []
    if do_lessons:
        lines.append("## Lessons from past builds (apply these):")
        for lesson in do_lessons:
            lines.append(f"- [{lesson.type}] When: {lesson.trigger} -> Do: {lesson.fix}")
    if dont_lessons:
        lines.append("\n## Anti-patterns from past builds (NEVER do these):")
        for lesson in dont_lessons:
            lines.append(f"- NEVER: {lesson.trigger} (Reason: {lesson.fix})")
    return "\n".join(lines)


def boost_confidence(lessons: list[Lesson], applied_triggers: list[str]):
    """Increase confidence for lessons that were applied in a successful build."""
    all_lessons = load_lessons()
    changed = False
    triggers_set = set(t.lower() for t in applied_triggers)
    for lesson in all_lessons:
        if lesson.trigger.lower() in triggers_set:
            lesson.confidence = min(1.0, lesson.confidence + 0.1)
            lesson.used += 1
            changed = True
    if changed:
        save_all(all_lessons)


def penalize_backfired(applied_triggers: list[str], error_text: str, max_penalty: float = 0.1) -> list[str]:
    """Decrement confidence for applied lessons whose trigger words appear in a fresh error.

    Returns the list of lesson trigger strings that were penalized (for emit/visibility).
    Only penalizes if >50% of the trigger's words appear in error_text.
    """
    if not applied_triggers or not error_text:
        return []
    error_words = set(re.findall(r"[a-z0-9_]+", error_text.lower()))
    backfired = []
    all_lessons = load_lessons()
    changed = False
    triggers_lower = {t.lower() for t in applied_triggers}
    for lesson in all_lessons:
        if lesson.trigger.lower() not in triggers_lower:
            continue
        trig_words = set(re.findall(r"[a-z0-9_]+", lesson.trigger.lower()))
        if not trig_words:
            continue
        overlap = len(trig_words & error_words) / len(trig_words)
        if overlap > 0.5:
            lesson.confidence = max(0.1, lesson.confidence - max_penalty)
            backfired.append(lesson.trigger)
            changed = True
    if changed:
        save_all(all_lessons)
    return backfired


def prune(min_confidence: float = 0.3, min_builds: int = 5):
    """Remove low-confidence lessons after they've had enough chances."""
    lessons = load_lessons()
    kept = [l for l in lessons if l.confidence >= min_confidence or l.used < min_builds]
    if len(kept) < len(lessons):
        save_all(kept)


def parse_reflection(text: str, tags: list[str] | None = None,
                       source_task: str = "") -> list[Lesson]:
    """Parse LLM reflection output into lessons.

    Formats:
        TYPE | TRIGGER | FIX           — positive lesson ("do")
        ANTI | TRIGGER | WHY           — anti-pattern ("dont")

    If `tags` is provided, every parsed lesson is tagged with that list (so
    future `recall()` can filter by language/framework/phase). If
    `source_task` is provided, every lesson captures the originating task
    text for cross-task decay during recall.

    New lessons start at confidence 0.3 (was 0.5). Three earlier successful
    reinforcements bring it to 0.6 before the lesson outweighs the noise
    floor in scoring — forces reuse before prominence.
    """
    lessons = []
    tags_list = list(tags) if tags else []
    for line in text.strip().split("\n"):
        line = line.strip().lstrip("- •")
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            lesson_type = parts[0].lower().replace(" ", "_")
            polarity = "do"
            if lesson_type == "anti":
                lesson_type = "error_pattern"
                polarity = "dont"
            valid_types = {"error_pattern", "architecture", "tool_pattern", "dependency", "performance"}
            if lesson_type not in valid_types:
                lesson_type = "error_pattern"
            # Backfill content-derived tags so fresh lessons never land
            # untagged — they get either the explicit tags from the caller
            # or whatever the lesson body's keywords imply.
            derived_tags = list(tags_list)
            if not derived_tags:
                body = parts[1] + " " + parts[2]
                derived_tags = sorted(infer_tags_from_text(body))
            lessons.append(Lesson(
                ts=time.time(),
                type=lesson_type,
                trigger=parts[1],
                fix=parts[2],
                confidence=0.3,
                polarity=polarity,
                tags=derived_tags,
                source_task=source_task[:200],
            ))
    return lessons


# ── Phase-budget history ─────────────────────────────────────────────────────
#
# Static `DEFAULT_BUDGETS` / `compute_budgets` formulas are guesses. Real past
# builds are a better signal: if BUILD has historically needed 45 rounds on
# pygame tasks, budgeting 30 guarantees a premature abort. If we only ever
# needed 12, budgeting 30 is waste.
#
# Each completed phase writes one row; `recall_phase_stats` reads back matching
# rows (same phase, tag overlap) and returns percentile stats the budget
# formula uses to scale up/down from its static baseline.

def record_phase_outcome(phase: str, rounds: int, n_files: int,
                         tags: list[str] | None = None) -> None:
    """Append one phase-completion record. Best-effort — never raises.

    Locked against concurrent appends so the JSONL stays parseable. (H2.)
    """
    try:
        entry = {
            "ts": time.time(),
            "phase": str(phase),
            "rounds": int(rounds),
            "n_files": int(n_files),
            "tags": list(tags or []),
        }
        from ._atomic import atomic_append_lines
        atomic_append_lines(PHASE_HISTORY_PATH, [json.dumps(entry)])
    except OSError:
        pass


def _load_phase_history() -> list[dict]:
    if not os.path.exists(PHASE_HISTORY_PATH):
        return []
    out: list[dict] = []
    try:
        with open(PHASE_HISTORY_PATH) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def recall_phase_stats(phase: str, tags: list[str] | None = None,
                       min_samples: int = 3) -> dict | None:
    """Return {mean, p90, max, count} for past runs of `phase` with matching tags.

    Tag matching: if `tags` is provided, prefer entries whose tag set overlaps.
    Falls back to ALL entries for this phase if the tagged subset has fewer
    than `min_samples` rows (so new task types still benefit from any history).
    Returns None when there's insufficient data to trust a stat.
    """
    history = _load_phase_history()
    if not history:
        return None
    phase_str = str(phase)
    same_phase = [h for h in history if h.get("phase") == phase_str]
    if not same_phase:
        return None
    if tags:
        # Tag-aware path: only use rows whose tags overlap the task's tags.
        # We DO NOT fall back to all entries when the tagged subset is thin —
        # inheriting pygame budgets for a react task is worse than no signal.
        tag_set = set(tags)
        matching = [h for h in same_phase if tag_set & set(h.get("tags") or [])]
    else:
        # No task_text was provided → every recorded run is a fair signal.
        matching = same_phase
    if len(matching) < min_samples:
        return None
    rounds = sorted(int(h.get("rounds", 0)) for h in matching)
    n = len(rounds)
    p90_idx = min(n - 1, int(n * 0.9))
    return {
        "mean": sum(rounds) / n,
        "p90": rounds[p90_idx],
        "max": rounds[-1],
        "count": n,
    }
