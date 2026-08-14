"""Secret and host-path redaction for anything Cadillac persists or publishes.

Cadillac had no redaction anywhere. Three sinks made that a real exposure
rather than a theoretical one:

  1. `.cadillac/build.jsonl` — every emitted event, verbatim, written INSIDE
     the workspace. The workspace is the deliverable: generated projects get
     published (matrix-doom went to GitHub), so anything the build printed
     travelled with it.
  2. `memory.jsonl` — lessons persist across every future build, so one
     leaked token is inherited by every subsequent run's prompt context.
  3. `progress.md` — written into the workspace for the same reason.

The rules below are deliberately conservative in the one direction that
matters: they must never mangle ordinary source code. A false positive here
corrupts a lesson or a log line; a false negative leaks a credential. So the
patterns match *shapes that are only ever secrets* (provider key prefixes,
`Authorization:` headers, assignments to KEY/TOKEN/SECRET-named variables)
rather than anything resembling a high-entropy string.

Home-path collapsing is separate and unconditional: `/home/waive3/...` becomes
`~/...` so published artifacts don't carry the host layout.
"""

from __future__ import annotations

import os
import re

REDACTED = "[REDACTED]"

# Provider key formats. Anchored on their documented prefixes so a random
# base64 blob in a test fixture is never touched.
_KEY_PATTERNS = [
    # OpenAI / Anthropic / Google / GitHub / Slack / AWS / HuggingFace
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bdop_v1_[a-f0-9]{60,}"),
    # JWT: three base64url segments. Distinctive enough to be safe.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]

# `Authorization: Bearer <token>` and friends — redact the value, keep the shape
# so a log line still reads as an auth header.
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|api-key|proxy-authorization)\b(\s*[:=]\s*)"
    r"(?:(bearer|basic|token)\s+)?([^\s'\"},;]+)"
)

# Assignments to credential-named variables, in any of the syntaxes a build
# touches: env files, shell exports, JSON, Python, JS.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|"
    r"PRIVATE[_-]?KEY|ACCESS[_-]?KEY)[A-Z0-9_]*)"
    # The optional quote absorbs a JSON key's closing quote: in
    # `{"jwt_secret": "..."}` the `"` sits between the name and the colon.
    r"(['\"]?\s*[:=]\s*)"
    r"(['\"]?)([^\s'\"},;\n]{4,})(['\"]?)"
)

# PEM private key blocks — collapse the whole body, not just a line.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Values that are obviously not secrets even in a credential-named field.
# Keeps generated config readable: `API_KEY=your-api-key-here` stays legible.
_PLACEHOLDER_VALUES = frozenset({
    "none", "null", "nil", "true", "false", "undefined", "changeme",
    "your-api-key", "your-api-key-here", "your_api_key", "xxx", "todo",
    "placeholder", "example", "test", "dummy", "fake", "secret",
    "process.env", "os.environ", "redacted",
})


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("'\"").lower()
    if v in _PLACEHOLDER_VALUES:
        return True
    # Env-var indirection (`API_KEY=$OPENAI_API_KEY`, `${VAR}`) is a reference,
    # not a value.
    if v.startswith(("$", "{", "<", "process.env", "os.environ", "os.getenv")):
        return True
    # All-same-char or very short values carry no secret.
    return len(v) < 8 or len(set(v)) <= 2


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings with a stable marker."""
    if not text or not isinstance(text, str):
        return text
    out = _PEM_RE.sub(f"{REDACTED}-PRIVATE-KEY", text)
    for pattern in _KEY_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _HEADER_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}"
                  f"{(m.group(3) + ' ') if m.group(3) else ''}{REDACTED}",
        out,
    )

    def _assignment(m):
        name, sep, q1, value, q2 = m.groups()
        if _is_placeholder(value):
            return m.group(0)
        return f"{name}{sep}{q1}{REDACTED}{q2}"

    return _ASSIGNMENT_RE.sub(_assignment, out)


def redact_home_path(text: str) -> str:
    """Collapse the host home directory to `~` so published artifacts don't
    carry the host's filesystem layout."""
    if not text or not isinstance(text, str):
        return text
    home = os.path.expanduser("~")
    if home and home != "/" and home in text:
        text = text.replace(home, "~")
    return text


def redact(text: str) -> str:
    """Full redaction pass: secrets, then host paths."""
    return redact_home_path(redact_secrets(text))


def redact_obj(obj, _depth: int = 0):
    """Recursively redact strings inside a JSON-serializable structure.

    Used by BuildLogger so every event field is covered without the emitter
    needing to know which fields might carry output. Depth-bounded so a
    pathological structure can't blow the stack.
    """
    if _depth > 12:
        return obj
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        redacted = [redact_obj(v, _depth + 1) for v in obj]
        return type(obj)(redacted) if isinstance(obj, tuple) else redacted
    return obj
