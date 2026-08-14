"""Stable probe suites across RUNTIME cycles.

RUNTIME can fire several times in one build (it bounces to BUILD when probes
fail, then re-runs). Every runner regenerated its probe set from the LLM on
each pass, so a real build produced:

    probes_run=16 failures=12
    probes_run=12 failures=10

Those two lines are not comparable — it is a *different suite*. You cannot tell
improvement from probe drift, and nothing downstream can notice "the same
probes have failed three cycles running", which is exactly the signal the
stuck-loop detector needs.

Caching is keyed on a fingerprint of the spec the probes were generated from,
so a widened spec (the progressive must → should → could tiers) correctly
regenerates while a re-run against an unchanged spec reuses the same suite.
"""

from __future__ import annotations

import hashlib
import json
import os


def spec_fingerprint(spec) -> str:
    """Stable hash of the spec a probe suite was generated from."""
    try:
        text = spec.to_prompt_block()
    except Exception:
        text = repr(spec)
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _path(workspace: str, name: str) -> str:
    return os.path.join(workspace, ".cadillac", name)


def load(workspace: str, name: str, spec) -> list | None:
    """Return the cached probe dicts when they match this spec, else None.

    Tolerates the older on-disk format (a bare list with no fingerprint) by
    treating it as a miss — regenerating is always safe, reusing a suite built
    from a different spec is not.
    """
    try:
        with open(_path(workspace, name)) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None  # legacy bare-list format
    if payload.get("spec_fingerprint") != spec_fingerprint(spec):
        return None
    probes = payload.get("probes")
    return probes if isinstance(probes, list) and probes else None


def save(workspace: str, name: str, spec, probes: list) -> None:
    """Persist a probe suite with the fingerprint of its originating spec."""
    try:
        cad_dir = os.path.join(workspace, ".cadillac")
        os.makedirs(cad_dir, exist_ok=True)
        from .._atomic import atomic_write_text
        atomic_write_text(
            _path(workspace, name),
            json.dumps(
                {"spec_fingerprint": spec_fingerprint(spec), "probes": probes},
                indent=2,
            ),
        )
    except (OSError, ImportError):
        pass
