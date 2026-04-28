"""API contracts as first-class artifacts.

The PLAN phase emits a `Contract` for any project where modules talk to each
other over HTTP (frontend↔backend). Both sides import from the same source —
the contract — instead of independently restating endpoint paths, request
shapes, and response shapes. This eliminates an entire class of bug we used
to ship: response-shape mismatches, validation-rule drift, frontend calling
a route that the backend never implemented.

The contract is intentionally simpler than full OpenAPI — the LLM has to
generate it, so the schema is a flat list of endpoints with stringly-typed
field names. It's enough to:

  - drive the WIRING phase's route inventory (no regex needed)
  - statically verify backend has every endpoint, frontend calls every endpoint
  - feed module BUILD prompts so the frontend module sees the shape the
    backend module is committed to (and vice versa)

Format on disk: `workspace/contracts.json`. JSON not YAML — no extra dep,
LLM emits it natively.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict


@dataclass
class Endpoint:
    """One HTTP endpoint in the contract.

    `request` and `response` are dicts of {field_name: type_string}. Type
    strings are informal ("string", "integer", "User", "array<Listing>"); the
    contract is for alignment, not strict validation.
    """

    name: str                            # "register", "place_bid"
    method: str                          # "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
    path: str                            # "/api/auth/register"
    module: str                          # owning backend module: "auth"
    consumed_by: list[str] = field(default_factory=list)   # frontend module(s)
    request: dict = field(default_factory=dict)            # {field: type_str}
    response: dict = field(default_factory=dict)           # {status_code: shape}
    description: str = ""

    def normalized_path(self) -> str:
        """Path with `<param>` placeholders normalized for matching."""
        return re.sub(r"<[^>]+>", "<param>", self.path)


@dataclass
class Contract:
    """Full project contract: endpoints + named types."""

    endpoints: list[Endpoint] = field(default_factory=list)
    types: dict = field(default_factory=dict)              # {TypeName: {field: type_str}}

    def to_dict(self) -> dict:
        return {
            "endpoints": [asdict(e) for e in self.endpoints],
            "types": self.types,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Contract":
        if not data:
            return cls()
        endpoints = []
        for raw in data.get("endpoints", []) or []:
            try:
                endpoints.append(Endpoint(
                    name=raw.get("name", ""),
                    method=(raw.get("method") or "GET").upper(),
                    path=raw.get("path", ""),
                    module=raw.get("module", ""),
                    consumed_by=list(raw.get("consumed_by", []) or []),
                    request=dict(raw.get("request") or {}),
                    response=dict(raw.get("response") or {}),
                    description=raw.get("description", ""),
                ))
            except Exception:
                continue
        return cls(endpoints=endpoints, types=dict(data.get("types") or {}))

    @classmethod
    def from_plan(cls, plan: dict) -> "Contract":
        """Pull contract out of a PLAN JSON dict (under the `contracts` key)."""
        return cls.from_dict(plan.get("contracts"))

    @classmethod
    def load(cls, workspace: str) -> "Contract":
        path = os.path.join(workspace, "contracts.json")
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path) as f:
                return cls.from_dict(json.load(f))
        except Exception:
            return cls()

    def save(self, workspace: str) -> str:
        path = os.path.join(workspace, "contracts.json")
        with open(path, "w") as f:
            f.write(self.to_json())
        return path

    def is_empty(self) -> bool:
        return not self.endpoints and not self.types

    # ── Filters ──────────────────────────────────────────────────────────

    def endpoints_for_module(self, module_name: str) -> list[Endpoint]:
        """Endpoints implemented by `module_name` (backend side)."""
        return [e for e in self.endpoints if e.module == module_name]

    def endpoints_consumed_by(self, module_name: str) -> list[Endpoint]:
        """Endpoints `module_name` is allowed to call (frontend side)."""
        return [e for e in self.endpoints if module_name in e.consumed_by]

    # ── Prompt rendering ─────────────────────────────────────────────────

    def render_for_module(self, module_name: str, *, kind: str = "auto") -> str:
        """Render a compact contract excerpt for inclusion in a module prompt.

        `kind` is "backend" (show owned endpoints with full request/response),
        "frontend" (show consumed endpoints with the same), or "auto" (both,
        deduped).
        """
        if self.is_empty():
            return ""
        owned = self.endpoints_for_module(module_name) if kind in ("backend", "auto") else []
        consumed = self.endpoints_consumed_by(module_name) if kind in ("frontend", "auto") else []
        seen: set[str] = set()
        merged: list[Endpoint] = []
        for e in (*owned, *consumed):
            key = f"{e.method} {e.path}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(e)
        if not merged and not self.types:
            return ""

        lines = [
            "## API Contract (authoritative — implement/consume EXACTLY these shapes)",
            "",
        ]
        for e in merged:
            lines.append(f"### {e.method} {e.path}  ({e.name})")
            if e.description:
                lines.append(f"_{e.description}_")
            if e.request:
                lines.append("Request body:")
                for k, t in e.request.items():
                    lines.append(f"  - {k}: {t}")
            if e.response:
                lines.append("Responses:")
                for code, shape in e.response.items():
                    if isinstance(shape, dict):
                        shape_str = ", ".join(f"{k}: {v}" for k, v in shape.items())
                    else:
                        shape_str = str(shape)
                    lines.append(f"  - {code}: {{{shape_str}}}")
            lines.append("")
        if self.types:
            lines.append("### Named types")
            for name, shape in self.types.items():
                if isinstance(shape, dict):
                    shape_str = ", ".join(f"{k}: {v}" for k, v in shape.items())
                else:
                    shape_str = str(shape)
                lines.append(f"  - {name}: {{{shape_str}}}")
        return "\n".join(lines)

    def render_full(self) -> str:
        """Render the entire contract (used in INTEGRATE / global prompts)."""
        if self.is_empty():
            return ""
        lines = ["## Full API Contract", ""]
        for e in self.endpoints:
            consumed = f"  (consumed by: {', '.join(e.consumed_by)})" if e.consumed_by else ""
            lines.append(f"- {e.method} {e.path}  →  {e.module}{consumed}")
        if self.types:
            lines.append("")
            lines.append("Types: " + ", ".join(self.types.keys()))
        return "\n".join(lines)
