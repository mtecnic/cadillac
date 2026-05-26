"""Spec — expand a terse task into explicit user stories before PLAN.

The recurring failure mode: user types "build an eBay clone", the LLM picks
its own interpretation of scope, ships a 30%-complete app, and validation
passes because everything the LLM built does work. Spec closes that gap.

A spec is an array of `Story` objects: each one is a discrete user-visible
capability with acceptance criteria. The PLAN phase sees the spec and builds
against it. The COMPLETENESS critic (separate module) later checks coverage.

The spec is generated once at the start of a build via an LLM call with the
task as input and a structured-output prompt. Persisted to `<workspace>/
spec.json` and loaded by downstream phases. Re-runs in `iterate` mode use
the existing spec rather than regenerating (drift would silently change
the build's target).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field

from ._atomic import atomic_write_text


_PRIORITY_VALUES = ("must", "should", "could")


@dataclass(frozen=True)
class Story:
    """One user-visible capability.

    Priority is MoSCoW-flavored:
      - "must": shipping without this means the project does not exist
      - "should": expected by users, but the product can launch without it
      - "could": nice-to-have, lowest weight in completeness scoring
    """
    id: str
    title: str
    acceptance: tuple[str, ...]      # "given X, when Y, then Z"-style assertions
    priority: str = "must"           # must | should | could
    category: str = ""               # "auth", "checkout", "search", etc.

    def to_dict(self) -> dict:
        d = asdict(self)
        d["acceptance"] = list(self.acceptance)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Story":
        priority = d.get("priority", "must")
        if priority not in _PRIORITY_VALUES:
            priority = "must"
        accept = d.get("acceptance") or []
        if isinstance(accept, str):
            accept = [accept]
        return cls(
            id=str(d.get("id", "")).strip(),
            title=str(d.get("title", "")).strip(),
            acceptance=tuple(str(a).strip() for a in accept if str(a).strip()),
            priority=priority,
            category=str(d.get("category", "")).strip(),
        )


@dataclass
class Spec:
    """The full spec for a build: the task plus its expanded user stories."""
    task: str
    stories: list[Story] = field(default_factory=list)
    generated_at: float = 0.0
    model: str = ""

    # ── Serialization ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "generated_at": self.generated_at,
            "model": self.model,
            "stories": [s.to_dict() for s in self.stories],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Spec":
        return cls(
            task=str(d.get("task", "")),
            generated_at=float(d.get("generated_at") or 0.0),
            model=str(d.get("model", "")),
            stories=[Story.from_dict(s) for s in (d.get("stories") or [])],
        )

    def save(self, workspace: str) -> str:
        path = os.path.join(workspace, "spec.json")
        atomic_write_text(path, json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, workspace: str) -> "Spec | None":
        path = os.path.join(workspace, "spec.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                return cls.from_dict(json.load(f))
        except (OSError, json.JSONDecodeError):
            return None

    # ── Convenience views ────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        return not self.stories

    def must_stories(self) -> list[Story]:
        return [s for s in self.stories if s.priority == "must"]

    def subset(self, priorities: tuple[str, ...]) -> "Spec":
        """Return a Spec view filtered to the given priorities.

        Used by the progressive-tier orchestration in engine.run() to scope
        each build pass to a priority slice (`("must",)`, then
        `("must", "should")`, etc.) without mutating the canonical
        spec.json on disk. Generated_at and model are preserved so the
        subset is still self-describing.
        """
        allowed = set(priorities)
        return Spec(
            task=self.task,
            generated_at=self.generated_at,
            model=self.model,
            stories=[s for s in self.stories if s.priority in allowed],
        )

    def to_prompt_block(self, max_chars: int = 4000) -> str:
        """Render the spec as a section drop into the architecture/build prompt.

        Truncates at `max_chars`. Must-priority stories always come first; they
        survive truncation even when the long tail is dropped, because the
        prompt budget can't allow a 50-story spec to crowd out the rest of the
        system prompt.
        """
        if not self.stories:
            return ""
        ordered = sorted(
            self.stories,
            key=lambda s: (_PRIORITY_VALUES.index(s.priority)
                           if s.priority in _PRIORITY_VALUES else 99,
                           s.id),
        )
        lines = ["## USER STORIES (build against this spec)"]
        used = len(lines[0])
        for st in ordered:
            chunk_lines = [
                f"\n### {st.id} [{st.priority}] {st.title}",
            ]
            if st.category:
                chunk_lines.append(f"  category: {st.category}")
            for a in st.acceptance:
                chunk_lines.append(f"  - {a}")
            chunk = "\n".join(chunk_lines)
            if used + len(chunk) > max_chars:
                # Truncate but never drop a must-priority story silently.
                if st.priority == "must":
                    lines.append(chunk)
                    used += len(chunk)
                else:
                    remaining_non_must = sum(
                        1 for s in ordered if s.priority != "must"
                    ) - sum(
                        1 for ln in lines if ln.startswith("\n### ")
                        and "[must]" not in ln
                    )
                    lines.append(
                        f"\n[{remaining_non_must} additional should/could "
                        f"stories omitted for prompt budget]"
                    )
                    break
            else:
                lines.append(chunk)
                used += len(chunk)
        return "\n".join(lines)


# ── LLM-driven generation ─────────────────────────────────────────────────────


_SPEC_SYSTEM_PROMPT = """You are a senior product engineer expanding a terse \
task description into a complete spec. Output a JSON object — no prose, no \
markdown fences.

Schema:
{
  "stories": [
    {
      "id": "S01",
      "title": "User signs up with email + password",
      "acceptance": [
        "POST /signup with valid email + 8+ char password returns 201",
        "Duplicate email returns 409 with explicit error message",
        "Password is hashed (not stored plaintext) before persisting"
      ],
      "priority": "must",
      "category": "auth"
    }
  ]
}

Rules:
- IDs are S01, S02, S03... (zero-padded, sequential).
- Priorities: "must" (product is broken without it), "should" (users expect \
it), "could" (nice-to-have). Be honest — most stories are "should", not "must".
- Acceptance items are testable, observable behaviors. "User can log in" is \
bad; "POST /login with valid creds returns 200 + session cookie" is good.
- Cover the OBVIOUS gaps a junior would miss: empty states, error states, \
logout, password reset, validation errors with messages, pagination on lists, \
loading/disabled states on async actions, server-side input validation.
- 15-40 stories for typical apps. CLIs may be shorter; e-commerce/multi-user \
apps will trend longer. Don't pad.
- Categories help with later coverage analysis. Reuse them across stories \
(e.g., multiple stories share category "auth").

Output ONLY the JSON object."""


def generate_spec(task: str, cfg, lang, emit=None) -> Spec:
    """Call the LLM to expand `task` into a Spec. Returns a Spec; never raises.

    On any failure (LLM unreachable, malformed JSON, empty story list), returns
    a Spec with an empty story list and a sentinel marker — downstream phases
    treat that as "no spec, fall back to old behavior".
    """
    from .engine import chat, extract_json

    if emit is None:
        emit = lambda *a, **kw: None

    user_msg = (
        f"TASK: {task}\n\n"
        f"TARGET LANGUAGE/STACK: {getattr(lang, 'name', 'python')}\n"
        f"FAMILY: {getattr(lang, 'family', 'python')}\n\n"
        "Expand this into a complete user-story spec following the schema and "
        "rules in the system prompt. Output JSON only."
    )
    messages = [
        {"role": "system", "content": _SPEC_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    emit("log", msg="[SPEC] expanding task into user stories...")
    try:
        msg = chat(cfg, messages, tools=[], emit=emit)
    except Exception as e:
        emit("log", msg=f"[SPEC] LLM error: {e}; continuing without spec")
        return Spec(task=task, generated_at=time.time(), model=cfg.model or "")
    raw = (msg.get("content") or "").strip()
    parsed = extract_json(raw)
    spec = Spec(task=task, generated_at=time.time(), model=cfg.model or "")
    if not parsed:
        emit("log", msg="[SPEC] LLM did not return parseable JSON; continuing without spec")
        return spec
    raw_stories = parsed.get("stories") if isinstance(parsed, dict) else None
    if not isinstance(raw_stories, list):
        emit("log", msg="[SPEC] response missing 'stories' array; continuing without spec")
        return spec
    seen_ids: set[str] = set()
    for entry in raw_stories:
        if not isinstance(entry, dict):
            continue
        story = Story.from_dict(entry)
        if not story.title or not story.id:
            continue
        if story.id in seen_ids:
            continue
        seen_ids.add(story.id)
        spec.stories.append(story)
    n_must = sum(1 for s in spec.stories if s.priority == "must")
    emit("log", msg=f"[SPEC] {len(spec.stories)} stories ({n_must} must)")
    return spec


# ── Coverage heuristic (cheap, static) ────────────────────────────────────────


def coverage_report(spec: Spec, workspace: str) -> dict:
    """For each story, decide whether *something* in the codebase plausibly
    addresses it. Returns {"covered": [...], "missing": [...], "score": float}.

    The heuristic: extract content-words from the story title + first
    acceptance line, search the workspace for any file whose path or content
    contains those words. Coverage is keyword-level only — it doesn't verify
    the story is correctly *implemented*, just that the LLM tried. Real
    behavioral verification is the critic's job.

    Skipped dirs: node_modules, dist, build, .venv, frontend/build artifacts.
    """
    if spec.is_empty():
        return {"covered": [], "missing": [], "score": 1.0}

    haystack = _collect_searchable_text(workspace)
    covered: list[str] = []
    missing: list[str] = []
    for story in spec.stories:
        keywords = _extract_keywords(story)
        if not keywords:
            covered.append(story.id)
            continue
        hits = sum(1 for kw in keywords if kw in haystack)
        # Half of the content-words must appear somewhere; rounded up.
        threshold = max(1, (len(keywords) + 1) // 2)
        if hits >= threshold:
            covered.append(story.id)
        else:
            missing.append(story.id)
    total = len(spec.stories)
    score = len(covered) / total if total else 1.0
    return {"covered": covered, "missing": missing, "score": score}


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "for", "to", "of",
    "in", "on", "with", "is", "are", "be", "user", "users", "can", "should",
    "must", "will", "shall", "may", "this", "that", "their", "there", "when",
    "given", "valid", "invalid", "returns", "return", "post", "get", "put",
    "delete", "page", "view", "show", "display", "shows", "displays",
    "request", "response", "json", "html", "css", "javascript", "python",
    "typescript", "data", "list", "item", "items", "form", "field", "fields",
    "system", "application", "app", "feature", "function", "method",
}


def _extract_keywords(story: Story) -> list[str]:
    """Pull lowercase content-words from title + first acceptance line."""
    import re
    text = story.title.lower()
    if story.acceptance:
        text += " " + story.acceptance[0].lower()
    raw = re.findall(r"[a-z][a-z0-9_-]{2,}", text)
    return [w for w in raw if w not in _STOPWORDS]


def _collect_searchable_text(workspace: str) -> str:
    """Concatenate file paths + source text into one lowercased blob."""
    parts: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", ".git", "__pycache__",
                                "dist", "build", "venv", ".venv",
                                ".cadillac", "coverage", ".pytest_cache")]
        for fn in files:
            if not _is_searchable(fn):
                continue
            path = os.path.join(root, fn)
            parts.append(os.path.relpath(path, workspace).lower())
            try:
                with open(path, errors="ignore") as f:
                    parts.append(f.read().lower())
            except OSError:
                continue
    return "\n".join(parts)


_SEARCHABLE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".rs", ".go",
    ".html", ".css", ".scss", ".php", ".rb", ".java", ".kt", ".swift",
    ".md", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sql",
}


def _is_searchable(fn: str) -> bool:
    ext = os.path.splitext(fn)[1].lower()
    return ext in _SEARCHABLE_EXTS
