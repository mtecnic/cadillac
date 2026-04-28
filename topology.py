"""Runtime topology — the orchestrator-owned cross-module graph.

`Topology` wraps `ModularPlan` + `Contract` + scanned-from-code facts (env-var
reads, port bindings, route registrations). It's the thing that lets us
ask cross-cutting questions the per-layer validators can't:

    - Is every env var that code reads either loaded somewhere or explicitly
      declared as a runtime input? (catches "config-key is dead lookup" bugs)
    - Does every contract endpoint's `module` reference a real module?
    - Does every `consumed_by` reference a real module?
    - Does any backend module collide with another on a fixed port?

Phase 3 of the architecture upgrade. Where Phase 1 (WIRING) is "boot it and
poke it" and Phase 2 (Contracts) is "agree on shapes," Phase 3 is "make sure
the modules can plug into each other coherently in the first place."
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .contracts import Contract, Endpoint
from .modules import ModularPlan, ModuleSpec


@dataclass
class TopologyIssue:
    """A single topology-level problem."""

    rule: str                       # "env_var_unloaded", "phantom_module", etc.
    severity: str                   # "error" | "warning"
    message: str
    refs: list[str] = field(default_factory=list)   # ["file.py:42", ...]


@dataclass
class Topology:
    """Bundle of plan + contract + scanned facts. Built once per workspace."""

    plan: ModularPlan | None
    contract: Contract
    workspace: str
    env_var_reads: list[tuple[str, str, bool]] = field(default_factory=list)
    # ^ (var_name, file:line, has_default)
    env_var_writes: list[tuple[str, str]] = field(default_factory=list)
    # ^ (var_name, file:line) — sites that set/load env vars
    port_bindings: list[tuple[int, str]] = field(default_factory=list)
    # ^ (port, file:line)
    config_key_reads: list[tuple[str, str, bool]] = field(default_factory=list)
    # ^ (key_name, file:line, has_default) — Flask app.config reads
    config_key_writes: list[tuple[str, str]] = field(default_factory=list)
    # ^ (key_name, file:line) — Flask app.config writes

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def build(cls, workspace: str, plan: ModularPlan | None = None,
              contract: Contract | None = None) -> "Topology":
        """Build a topology by scanning the workspace + loading plan/contract.

        `plan` and `contract` can be passed in if the caller already has them
        in memory; otherwise loaded from disk.
        """
        if contract is None:
            contract = Contract.load(workspace)
        if plan is None:
            plan = _try_load_modular_plan(workspace)

        env_reads, env_writes, ports, cfg_reads, cfg_writes = _scan_python(workspace)

        return cls(
            plan=plan,
            contract=contract,
            workspace=workspace,
            env_var_reads=env_reads,
            env_var_writes=env_writes,
            port_bindings=ports,
            config_key_reads=cfg_reads,
            config_key_writes=cfg_writes,
        )

    # ── Queries ─────────────────────────────────────────────────────────

    def module_names(self) -> set[str]:
        if not self.plan:
            return set()
        return {m.name for m in self.plan.modules}

    def env_vars_read(self, *, only_no_default: bool = False) -> set[str]:
        return {v for v, _, has_def in self.env_var_reads
                if not only_no_default or not has_def}

    def env_vars_loaded(self) -> set[str]:
        return {v for v, _ in self.env_var_writes}

    # ── Invariant checks ─────────────────────────────────────────────────

    def check(self) -> list[TopologyIssue]:
        """Run every topology invariant. Returns empty list if clean."""
        issues: list[TopologyIssue] = []
        issues.extend(self._check_env_vars())
        issues.extend(self._check_config_keys())
        issues.extend(self._check_contract_module_refs())
        issues.extend(self._check_port_conflicts())
        return issues

    def _check_config_keys(self) -> list[TopologyIssue]:
        """Every default-less `app.config.get("X")` should have a write site
        somewhere — `app.config["X"] = ...`, `app.config.from_object(...)`,
        or `app.config.update(...)`. A read with no corresponding write is
        a dead lookup that always returns None.

        Bug class this catches: backend reads `CORS_ORIGINS` from app.config
        but nothing ever stores it there, so the lookup falls back to its
        default — silently breaking real deploys.
        """
        issues: list[TopologyIssue] = []
        # If any from_object / update / from_envvar / from_pyfile call exists
        # anywhere, treat config as bulk-loaded — we can't statically know
        # which keys those provide.
        bulk_loaded = any(
            ref.endswith("__BULK_LOAD__") or key == "__BULK_LOAD__"
            for key, ref in self.config_key_writes
        )
        if bulk_loaded:
            return issues
        written = {k for k, _ in self.config_key_writes}
        seen: dict[str, list[str]] = {}
        for key, ref, has_default in self.config_key_reads:
            if has_default or key in written:
                continue
            seen.setdefault(key, []).append(ref)
        for key, refs in seen.items():
            issues.append(TopologyIssue(
                rule="config_key_dead_lookup",
                severity="error",
                message=(
                    f"`app.config.get(\"{key}\")` is read with no default and no "
                    f"corresponding `app.config[\"{key}\"] = ...` write site. "
                    f"This lookup will always return None. Either set it from an "
                    f"env var, give it a sensible default, or use "
                    f"`app.config.from_object(...)` to bulk-load."
                ),
                refs=refs[:5],
            ))
        return issues

    def _check_env_vars(self) -> list[TopologyIssue]:
        """Every default-less `os.environ.get(X)` should either be loaded
        somewhere in code (dotenv, argv, manual set) OR documented as an
        expected runtime input via a known launcher script.

        Bug class this catches: backend reads `CORS_ORIGINS` but never loads
        it into config or sets a fallback that includes the real deploy host.
        """
        issues: list[TopologyIssue] = []
        loaded = self.env_vars_loaded()
        # If the project uses python-dotenv, defer to it — we can't statically
        # know what `.env` will contain.
        if "__DOTENV_LOAD__" in loaded:
            return issues
        # System-managed and infrastructure vars — don't flag these.
        ambient = {
            "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "PWD",
            "PORT", "HOST", "FLASK_ENV", "FLASK_DEBUG", "PYTHONPATH",
            "NODE_ENV", "DEBUG", "TERM", "TZ", "DATABASE_URL", "SECRET_KEY",
            "VIRTUAL_ENV",
        }
        seen_var: dict[str, list[str]] = {}
        for var, ref, has_default in self.env_var_reads:
            if has_default or var in ambient or var in loaded:
                continue
            seen_var.setdefault(var, []).append(ref)
        for var, refs in seen_var.items():
            issues.append(TopologyIssue(
                rule="env_var_unloaded",
                severity="error",
                message=(
                    f"`{var}` is read via os.environ.get with no default and no "
                    f"corresponding load site. Either give it a sensible default, "
                    f"document it as a required input, or load it explicitly via "
                    f"`load_dotenv()` / argv parsing."
                ),
                refs=refs[:5],
            ))
        return issues

    def _check_contract_module_refs(self) -> list[TopologyIssue]:
        """Every endpoint's `module` and `consumed_by` should reference an
        actual module in the plan. Phantom references mean the contract was
        written against an architecture that doesn't exist."""
        if self.contract.is_empty() or not self.plan:
            return []
        names = self.module_names()
        if not names:
            return []
        issues: list[TopologyIssue] = []
        for ep in self.contract.endpoints:
            if ep.module and ep.module not in names:
                issues.append(TopologyIssue(
                    rule="phantom_module_owner",
                    severity="error",
                    message=(
                        f"Contract endpoint {ep.method} {ep.path} declares "
                        f"module='{ep.module}', but no such module exists in the "
                        f"plan. Known modules: {sorted(names)}."
                    ),
                ))
            for consumer in ep.consumed_by:
                if consumer not in names:
                    issues.append(TopologyIssue(
                        rule="phantom_module_consumer",
                        severity="error",
                        message=(
                            f"Contract endpoint {ep.method} {ep.path} declares "
                            f"consumed_by includes '{consumer}', but no such "
                            f"module exists. Known modules: {sorted(names)}."
                        ),
                    ))
        return issues

    def _check_port_conflicts(self) -> list[TopologyIssue]:
        """If the codebase binds the same fixed port at two distinct call
        sites in different files, surface it. (Same file/multiple bindings
        is usually a fallback chain, not a conflict.)"""
        by_port: dict[int, list[str]] = {}
        for port, ref in self.port_bindings:
            by_port.setdefault(port, []).append(ref)
        issues: list[TopologyIssue] = []
        for port, refs in by_port.items():
            files = {r.split(":")[0] for r in refs}
            if len(files) > 1:
                issues.append(TopologyIssue(
                    rule="port_conflict",
                    severity="warning",
                    message=(
                        f"Port {port} is bound from {len(files)} different files. "
                        f"If both processes run together they'll fight for the port."
                    ),
                    refs=refs,
                ))
        return issues


# ── Internal scanners ───────────────────────────────────────────────────────

def _try_load_modular_plan(workspace: str) -> ModularPlan | None:
    """Best-effort plan reload from `workspace/plan.json`. Returns None if
    the plan isn't modular or can't be parsed."""
    import json
    path = os.path.join(workspace, "plan.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    if not data.get("modular"):
        return None
    try:
        return ModularPlan.from_dict(data)
    except Exception:
        return None


def _scan_python(workspace: str) -> tuple[
    list[tuple[str, str, bool]],
    list[tuple[str, str]],
    list[tuple[int, str]],
    list[tuple[str, str, bool]],
    list[tuple[str, str]],
]:
    """Walk all .py files.

    Returns (env_reads, env_writes, port_bindings, config_reads, config_writes).
    """
    env_reads: list[tuple[str, str, bool]] = []
    env_writes: list[tuple[str, str]] = []
    port_bindings: list[tuple[int, str]] = []
    config_reads: list[tuple[str, str, bool]] = []
    config_writes: list[tuple[str, str]] = []

    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", "frontend", ".git",
                                "__pycache__", "dist", "build", "venv", ".venv",
                                ".cadillac", ".pytest_cache")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path) as f:
                    text = f.read()
            except Exception:
                continue
            rel = os.path.relpath(path, workspace)

            # os.environ.get("X") — with optional second arg = has_default
            for m in re.finditer(
                r'os\.environ\.get\(\s*["\'](\w+)["\']\s*(,)?',
                text,
            ):
                var = m.group(1)
                has_default = bool(m.group(2))
                line_no = text[:m.start()].count("\n") + 1
                env_reads.append((var, f"{rel}:{line_no}", has_default))
            # os.environ["X"] (subscript form — always raises KeyError, treat as no-default)
            for m in re.finditer(r'os\.environ\[\s*["\'](\w+)["\']\s*\]', text):
                var = m.group(1)
                line_no = text[:m.start()].count("\n") + 1
                env_reads.append((var, f"{rel}:{line_no}", False))

            # Env-var load sites
            # os.environ["X"] = ... or os.environ.setdefault(...)
            for m in re.finditer(
                r'os\.environ\[\s*["\'](\w+)["\']\s*\]\s*=',
                text,
            ):
                var = m.group(1)
                line_no = text[:m.start()].count("\n") + 1
                env_writes.append((var, f"{rel}:{line_no}"))
            for m in re.finditer(
                r'os\.environ\.setdefault\(\s*["\'](\w+)["\']',
                text,
            ):
                var = m.group(1)
                line_no = text[:m.start()].count("\n") + 1
                env_writes.append((var, f"{rel}:{line_no}"))
            # load_dotenv() — registers any var defined in a .env file. We can't
            # statically know which vars; treat the call as a load-site for all
            # vars (signal: "loading is happening here") via a sentinel.
            if re.search(r'\bload_dotenv\s*\(', text):
                env_writes.append(("__DOTENV_LOAD__", f"{rel}"))

            # Hardcoded port literals: app.run(port=NNNN) / Popen(host, NNNN) / =NNNN
            for m in re.finditer(
                r'\b(?:port|PORT)\s*=\s*(\d{4,5})\b',
                text,
            ):
                port = int(m.group(1))
                if 1024 <= port <= 65535:
                    line_no = text[:m.start()].count("\n") + 1
                    port_bindings.append((port, f"{rel}:{line_no}"))

            # Flask app.config reads/writes. We look for `<name>.config.get(...)`
            # / `<name>.config["X"]` / `<name>.config["X"] = ...` and similar.
            for m in re.finditer(
                r'\.config\.get\(\s*["\'](\w+)["\']\s*(,)?',
                text,
            ):
                key = m.group(1)
                has_default = bool(m.group(2))
                line_no = text[:m.start()].count("\n") + 1
                config_reads.append((key, f"{rel}:{line_no}", has_default))
            for m in re.finditer(
                r'\.config\[\s*["\'](\w+)["\']\s*\]\s*(=)?',
                text,
            ):
                key = m.group(1)
                line_no = text[:m.start()].count("\n") + 1
                if m.group(2):
                    config_writes.append((key, f"{rel}:{line_no}"))
                else:
                    # bare subscript read (no default) — same as no-default get
                    config_reads.append((key, f"{rel}:{line_no}", False))
            # Bulk-load forms: from_object, from_pyfile, from_envvar, update.
            # We can't know which keys these provide, so emit a sentinel.
            if re.search(
                r'\.config\.(from_object|from_pyfile|from_envvar|from_mapping|update)\b',
                text,
            ):
                config_writes.append(("__BULK_LOAD__", f"{rel}"))

    return env_reads, env_writes, port_bindings, config_reads, config_writes
