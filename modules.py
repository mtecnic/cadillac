"""Module data model for large modular codebases.

Provides ModuleSpec, ModularPlan, topological validation, and affected-module
detection for scoped iterate.
"""

import ast
import os
import re
from dataclasses import dataclass, field


@dataclass
class ModuleSpec:
    """Specification for a single module within a modular project."""

    name: str                           # "auth", "storage", "api"
    path: str                           # "auth/" (relative dir prefix)
    purpose: str                        # one-line description
    files: list[dict] = field(default_factory=list)
    # [{path, purpose, exports, interfaces, depends_on}]
    depends_on: list[str] = field(default_factory=list)
    # module names this imports from
    exports: list[str] = field(default_factory=list)
    # public names ["AuthService", "Token"]
    interfaces: list[str] = field(default_factory=list)
    # signatures ["AuthService.login(email, pw) -> Token"]
    build_order: list[dict] = field(default_factory=list)
    # within-module file batches
    test_file: str | None = None        # "auth/test_auth.py"
    real_interfaces: dict = field(default_factory=dict)
    # Populated after the module successfully builds: {file_relpath: signature_text}
    # — derived by ast-parsing the module's public Python files. Used by downstream
    # modules instead of the comment-only `interfaces` stubs (which go stale).


@dataclass
class ModularPlan:
    """Plan for a modular project with multiple build phases."""

    modules: list[ModuleSpec] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)    # pip packages
    entry_point: str = "main.py"
    integration_files: list[str] = field(default_factory=list)  # ["main.py", "__init__.py"]
    module_build_order: list[list[str]] = field(default_factory=list)
    # [["core"], ["auth","storage"], ["api"]]
    test_file: str = "test_integration.py"

    def is_modular(self) -> bool:
        return len(self.modules) > 0

    def get_module(self, name: str) -> ModuleSpec | None:
        for m in self.modules:
            if m.name == name:
                return m
        return None

    def get_dependency_interfaces(self, module_name: str) -> str:
        """Build interface stubs for a module's dependencies.

        Prefers real ast-parsed signatures (populated after the dep module
        completes; see extract_real_interfaces). Falls back to the manifest's
        comment-only `exports`/`interfaces` fields when real signatures are
        absent (dep not yet built, or extraction failed).
        """
        mod = self.get_module(module_name)
        if not mod:
            return ""

        lines = []
        missing_deps: list[str] = []
        for dep_name in mod.depends_on:
            dep = self.get_module(dep_name)
            if not dep:
                # Phantom dependency — declared in depends_on but no such
                # module exists in the plan. Previously skipped silently;
                # the LLM then wrote code assuming an interface that's
                # never going to exist, which passed unit tests and broke
                # at INTEGRATE. Surface it loudly. (Audit H8.)
                missing_deps.append(dep_name)
                lines.append(f"# !! WARNING: dependency '{dep_name}' is "
                             f"declared in {module_name}.depends_on but no "
                             f"such module exists in the plan. Either fix "
                             f"the typo or remove the dependency.")
                continue
            lines.append(f"# --- Dependency: {dep_name} (from {dep.path}) ---")
            if dep.real_interfaces:
                # Render real signatures grouped by source file.
                for fpath, sig_text in dep.real_interfaces.items():
                    lines.append(f"# {fpath}")
                    for sig_line in sig_text.splitlines():
                        if sig_line.strip():
                            lines.append(f"#   {sig_line}")
            else:
                # Stale fallback: comments from manifest only.
                lines.append(f"# Exports: {', '.join(dep.exports)}")
                for iface in dep.interfaces:
                    lines.append(f"# {iface}")
            lines.append("")
        if missing_deps:
            # Also stash on the module so the engine can detect this and
            # halt the module build cleanly rather than letting it ship.
            mod.missing_deps = missing_deps  # type: ignore[attr-defined]
        return "\n".join(lines)

    def to_flat_plan(self) -> dict:
        """Convert to flat plan.json format for backward compatibility."""
        all_files = []
        all_build_order = []
        batch_num = 1

        for wave in self.module_build_order:
            for mod_name in wave:
                mod = self.get_module(mod_name)
                if not mod:
                    continue
                for f in mod.files:
                    all_files.append(f)
                for b in mod.build_order:
                    all_build_order.append({
                        "batch": batch_num,
                        "files": b.get("files", []),
                    })
                    batch_num += 1

        # Add integration files at the end
        for ifile in self.integration_files:
            all_files.append({"path": ifile, "purpose": "Integration wiring"})
            all_build_order.append({"batch": batch_num, "files": [ifile]})
            batch_num += 1

        # Add test file
        if self.test_file:
            all_files.append({"path": self.test_file, "purpose": "Integration tests"})
            all_build_order.append({"batch": batch_num, "files": [self.test_file]})

        return {
            "modular": True,
            "files": all_files,
            "dependencies": self.dependencies,
            "build_order": all_build_order,
            "entry_point": self.entry_point,
            "test_file": self.test_file,
            # Preserve module list for budget calculations — compute_budgets
            # scales INTEGRATE with n_modules, so the flat view needs this.
            "modules": [{"name": m.name} for m in self.modules],
        }

    @classmethod
    def from_dict(cls, data: dict, lang=None) -> "ModularPlan":
        """Construct a ModularPlan from a parsed manifest dict."""
        if lang and lang.family == "node":
            default_entry = "src/index.ts"
            default_test = "integration.test.ts"
        else:
            default_entry = "main.py"
            default_test = "test_integration.py"

        modules = []
        for m in data.get("modules", []):
            modules.append(ModuleSpec(
                name=m["name"],
                path=m.get("path", f"{m['name']}/"),
                purpose=m.get("purpose", ""),
                files=m.get("files", []),
                depends_on=m.get("depends_on", []),
                exports=m.get("exports", []),
                interfaces=m.get("interfaces", []),
                build_order=m.get("build_order", []),
                test_file=m.get("test_file"),
            ))

        return cls(
            modules=modules,
            dependencies=data.get("dependencies", []),
            entry_point=data.get("entry_point", default_entry),
            integration_files=data.get("integration_files", [default_entry]),
            module_build_order=data.get("module_build_order", []),
            test_file=data.get("test_file", default_test),
        )


# ── Validation ───────────────────────────────────────���───────────────────────

def validate_modular_plan(plan: dict) -> list[str]:
    """Validate a modular plan dict. Returns list of error strings (empty = valid)."""
    errors = []

    modules = plan.get("modules", [])
    if not modules:
        errors.append("No modules defined")
        return errors

    module_names = {m["name"] for m in modules}

    # Check for duplicate module names
    seen = set()
    for m in modules:
        if m["name"] in seen:
            errors.append(f"Duplicate module name: {m['name']}")
        seen.add(m["name"])

    # Check dependencies reference existing modules
    for m in modules:
        for dep in m.get("depends_on", []):
            if dep not in module_names:
                errors.append(f"Module '{m['name']}' depends on unknown module '{dep}'")

    # Check for circular dependencies
    cycle = _detect_cycle(modules)
    if cycle:
        errors.append(f"Circular dependency: {' -> '.join(cycle)}")

    # Check path prefix uniqueness
    paths = {}
    for m in modules:
        p = m.get("path", f"{m['name']}/").rstrip("/")
        if p in paths:
            errors.append(f"Duplicate path '{p}' for modules '{paths[p]}' and '{m['name']}'")
        paths[p] = m["name"]

    # Check module_build_order covers all modules
    build_order = plan.get("module_build_order", [])
    ordered_modules = set()
    for wave in build_order:
        for name in wave:
            if name not in module_names:
                errors.append(f"Build order references unknown module: {name}")
            ordered_modules.add(name)
    missing = module_names - ordered_modules
    if missing:
        errors.append(f"Modules missing from build order: {', '.join(sorted(missing))}")

    # Verify topological consistency: modules in later waves should only depend on earlier
    built = set()
    for wave in build_order:
        for name in wave:
            m = next((x for x in modules if x["name"] == name), None)
            if m:
                for dep in m.get("depends_on", []):
                    if dep not in built and dep not in wave:
                        errors.append(
                            f"Module '{name}' depends on '{dep}' which is not built in a prior wave"
                        )
        built.update(wave)

    # Check files have paths within module path
    for m in modules:
        mod_path = m.get("path", f"{m['name']}/").rstrip("/")
        for f in m.get("files", []):
            fpath = f.get("path", "") if isinstance(f, dict) else f
            if not fpath.startswith(mod_path + "/") and not fpath.startswith(mod_path + "\\"):
                errors.append(f"File '{fpath}' in module '{m['name']}' is outside module path '{mod_path}/'")

    return errors


def _detect_cycle(modules: list[dict]) -> list[str] | None:
    """Detect circular dependencies using DFS. Returns cycle path or None."""
    graph = {m["name"]: m.get("depends_on", []) for m in modules}
    visited = set()
    path = []
    path_set = set()

    def dfs(node: str) -> list[str] | None:
        if node in path_set:
            idx = path.index(node)
            return path[idx:] + [node]
        if node in visited:
            return None
        visited.add(node)
        path.append(node)
        path_set.add(node)
        for neighbor in graph.get(node, []):
            result = dfs(neighbor)
            if result:
                return result
        path.pop()
        path_set.remove(node)
        return None

    for name in graph:
        if name not in visited:
            result = dfs(name)
            if result:
                return result
    return None


def topological_sort(modules: list[dict]) -> list[list[str]]:
    """Compute a wave-based topological sort. Returns list of waves (parallel groups)."""
    graph = {m["name"]: set(m.get("depends_on", [])) for m in modules}
    all_names = set(graph.keys())
    waves = []
    built = set()

    while built != all_names:
        # Find modules whose deps are all built
        wave = [
            name for name in all_names - built
            if graph[name] <= built
        ]
        if not wave:
            # Remaining modules have unresolvable deps — include them anyway
            wave = sorted(all_names - built)
        waves.append(sorted(wave))
        built.update(wave)

    return waves


# ── Affected module detection for scoped iterate ─────────────────────────────

def detect_affected_modules(
    instruction: str,
    failure_text: str,
    plan: "ModularPlan",
    workspace: str,
) -> list[str]:
    """Detect which modules are affected by an instruction or failure.

    Uses file paths from tracebacks + keyword matching module names in instruction.
    Returns sorted list of module names.
    """
    affected = set()

    # Build module path -> name mapping
    path_to_module: dict[str, str] = {}
    for mod in plan.modules:
        mod_prefix = mod.path.rstrip("/")
        path_to_module[mod_prefix] = mod.name

    # 1. Parse file paths from failure tracebacks
    traceback_files = re.findall(r'File "([^"]+)"', failure_text)
    for fpath in traceback_files:
        # Get relative path
        if workspace and fpath.startswith(workspace):
            fpath = os.path.relpath(fpath, workspace)
        # Match against module paths
        for prefix, mod_name in path_to_module.items():
            if fpath.startswith(prefix + "/") or fpath.startswith(prefix + os.sep):
                affected.add(mod_name)

    # 2. Parse file:line patterns from pytest/jest output
    test_files = re.findall(r'(\S+\.(?:py|ts|js|tsx|jsx))::\w+', failure_text)
    # Also match Jest-style: "FAIL src/auth/auth.test.ts"
    test_files += re.findall(r'(?:FAIL|PASS)\s+(\S+\.(?:ts|js|tsx|jsx))', failure_text)
    for fpath in test_files:
        for prefix, mod_name in path_to_module.items():
            if fpath.startswith(prefix + "/"):
                affected.add(mod_name)

    # 3. Keyword match module names in instruction
    instruction_lower = instruction.lower()
    for mod in plan.modules:
        if mod.name.lower() in instruction_lower:
            affected.add(mod.name)

    # 4. If instruction mentions files, map them to modules
    file_refs = re.findall(r'(\S+\.(?:py|ts|js|tsx|jsx))', instruction)
    for fpath in file_refs:
        for prefix, mod_name in path_to_module.items():
            if fpath.startswith(prefix + "/"):
                affected.add(mod_name)

    # 5. Parse import errors to detect the SOURCE module being imported from
    #    e.g., "No module named 'core.types'" → flag 'core' module
    import_errors = re.findall(
        r"(?:No module named|cannot import name \S+ from) '(\w+)(?:\.\w+)*'",
        failure_text,
    )
    for pkg_name in import_errors:
        for prefix, mod_name in path_to_module.items():
            if os.path.basename(prefix) == pkg_name or mod_name == pkg_name:
                affected.add(mod_name)

    # If nothing detected, fall back to all modules
    if not affected:
        affected = {m.name for m in plan.modules}

    return sorted(affected)


# ── Real interface extraction (populates ModuleSpec.real_interfaces) ─────────

def extract_real_interfaces(workspace: str, mod: ModuleSpec) -> dict[str, str]:
    """Walk a completed module's public Python files and ast-parse signatures.

    Returns {file_relpath: signature_text} where signature_text contains real
    `def` and `class` lines (with their argument lists and return types) plus
    top-level constants. Underscore-prefixed names (private) are skipped.

    On any parse error the file is silently skipped. Non-Python files are
    ignored — for now this targets Python modular builds; TypeScript modules
    fall back to the comment-only stubs.
    """
    mod_dir = os.path.join(workspace, mod.path.rstrip("/"))
    if not os.path.isdir(mod_dir):
        return {}

    out: dict[str, str] = {}

    # Prefer __init__.py first (re-exports), then other public .py files.
    candidates = []
    init_path = os.path.join(mod_dir, "__init__.py")
    if os.path.isfile(init_path):
        candidates.append(init_path)
    for entry in sorted(os.listdir(mod_dir)):
        full = os.path.join(mod_dir, entry)
        if not os.path.isfile(full):
            continue
        if not entry.endswith(".py"):
            continue
        if entry.startswith("_") or entry == "__init__.py":
            continue
        # Skip test files
        if entry.startswith("test_") or entry.endswith("_test.py"):
            continue
        candidates.append(full)

    for full in candidates:
        relpath = os.path.relpath(full, workspace)
        sigs = _extract_signatures_from_file(full)
        if sigs:
            out[relpath] = sigs

    return out


def _extract_signatures_from_file(path: str) -> str:
    """Parse a Python file via ast and return public signatures as multi-line text."""
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return ""

    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            lines.append(_format_func_signature(node))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            lines.append(_format_class_signature(node))
            # Include public methods (one level down)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("_") and sub.name != "__init__":
                        continue
                    method_sig = _format_func_signature(sub, indent="    ")
                    lines.append(method_sig)
        elif isinstance(node, ast.Assign):
            # Top-level constants: NAME = ... — capture name only (value may be huge)
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_") and target.id.isupper():
                    lines.append(f"{target.id} = ...")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if not name.startswith("_"):
                try:
                    annotation = ast.unparse(node.annotation)
                except (AttributeError, ValueError):
                    annotation = "?"
                lines.append(f"{name}: {annotation}")
    return "\n".join(lines)


def _format_func_signature(node, indent: str = "") -> str:
    """Render a function/async def signature line (no body)."""
    try:
        args_str = ast.unparse(node.args)
    except (AttributeError, ValueError):
        args_str = "..."
    returns = ""
    if node.returns is not None:
        try:
            returns = f" -> {ast.unparse(node.returns)}"
        except (AttributeError, ValueError):
            returns = ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{indent}{prefix} {node.name}({args_str}){returns}: ..."


def _format_class_signature(node) -> str:
    """Render a class definition line with bases (no body)."""
    bases = []
    for b in node.bases:
        try:
            bases.append(ast.unparse(b))
        except (AttributeError, ValueError):
            continue
    base_part = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{base_part}: ..."
