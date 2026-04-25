"""Budget-aware hierarchical code map with AST-based skeleton + bookend rendering.

Provides the iterate() mode with a compact, refreshable view of the entire project
that eliminates the need for read_file calls. Uses bookend-style rendering (first/last
lines of functions) with real line numbers, plus full expansion of failure-relevant code.

Inspired by CTX (github.com/mtecnic/ctx) — structural representation with skip blocks.
"""

import ast
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class FunctionSkeleton:
    name: str
    lineno: int           # first line (decorator or def)
    end_lineno: int       # last line of body
    signature: str        # actual "def name(args) -> ret:" from source
    decorators: list[str] = field(default_factory=list)
    docstring: Optional[str] = None

@dataclass
class ClassSkeleton:
    name: str
    lineno: int
    end_lineno: int
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: Optional[str] = None
    attributes: list[str] = field(default_factory=list)
    methods: list[FunctionSkeleton] = field(default_factory=list)

@dataclass
class FileSkeleton:
    path: str
    total_lines: int
    source_lines: list[str] = field(default_factory=list)  # raw source lines (0-indexed)
    docstring: Optional[str] = None
    imports: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)
    classes: list[ClassSkeleton] = field(default_factory=list)
    functions: list[FunctionSkeleton] = field(default_factory=list)
    has_main_guard: bool = False
    content_hash: str = ""


# ── AST parsing ──────────────────────────────────────────────────────────────

def _extract_signature(source_lines: list[str], node: ast.FunctionDef) -> str:
    """Extract the actual function signature from source lines, preserving formatting."""
    start = node.lineno - 1  # 0-indexed
    # Scan backward for decorators
    sig_parts = []
    for i in range(start, min(start + 5, len(source_lines))):
        line = source_lines[i].rstrip()
        sig_parts.append(line)
        if line.rstrip().endswith(":"):
            break
    return "\n".join(sig_parts)


def _extract_docstring(node: ast.AST) -> Optional[str]:
    """Extract docstring from an AST node (module, class, or function)."""
    if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, (ast.Constant, ast.Str))):
        val = node.body[0].value
        doc = val.value if isinstance(val, ast.Constant) else val.s
        if isinstance(doc, str):
            # Truncate to first line or 80 chars
            first_line = doc.strip().split("\n")[0]
            return first_line[:80]
    return None


def _extract_decorators(source_lines: list[str], node: ast.AST) -> list[str]:
    """Extract decorator strings from source."""
    decos = []
    for deco in getattr(node, "decorator_list", []):
        line_idx = deco.lineno - 1
        if 0 <= line_idx < len(source_lines):
            decos.append(source_lines[line_idx].strip())
    return decos


def _extract_class_attributes(node: ast.ClassDef, source_lines: list[str]) -> list[str]:
    """Extract class-level attribute assignments."""
    attrs = []
    for child in node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            line = source_lines[child.lineno - 1].strip()
            attrs.append(line[:100])
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    line = source_lines[child.lineno - 1].strip()
                    attrs.append(line[:100])
    return attrs


_JS_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx", ".vue"})


def parse_file_to_skeleton(filepath: str) -> Optional[FileSkeleton]:
    """Parse a source file into a FileSkeleton.

    Routes to AST parser for Python, regex parser for JS/TS.
    Falls back to regex-based extraction if AST parsing fails.
    """
    _, ext = os.path.splitext(filepath)
    if ext in _JS_EXTENSIONS:
        return _parse_js_file_to_skeleton(filepath)
    if ext in (".html", ".css", ".scss"):
        # Non-code files: return minimal skeleton with line count only
        try:
            with open(filepath) as f:
                source = f.read()
        except OSError:
            return None
        lines = source.splitlines()
        return FileSkeleton(
            path=os.path.basename(filepath),
            total_lines=len(lines),
            source_lines=lines,
            content_hash=hashlib.md5(source.encode()).hexdigest(),
        )
    try:
        with open(filepath) as f:
            source = f.read()
    except OSError:
        return None

    lines = source.splitlines()
    content_hash = hashlib.md5(source.encode()).hexdigest()

    skel = FileSkeleton(
        path=os.path.basename(filepath),
        total_lines=len(lines),
        source_lines=lines,
        content_hash=content_hash,
    )

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        # Fallback: regex-based extraction
        return _parse_file_regex(filepath, lines, content_hash)

    skel.docstring = _extract_docstring(tree)

    # Extract imports, constants, classes, functions from top-level
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            line = lines[node.lineno - 1].strip()
            skel.imports.append(line)

        elif isinstance(node, ast.Assign):
            # Top-level constant assignments
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    line = lines[node.lineno - 1].strip()
                    if len(line) > 100:
                        line = line[:97] + "..."
                    skel.constants.append(line)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_skel = FunctionSkeleton(
                name=node.name,
                lineno=node.lineno,
                end_lineno=node.end_lineno or node.lineno,
                signature=_extract_signature(lines, node),
                decorators=_extract_decorators(lines, node),
                docstring=_extract_docstring(node),
            )
            skel.functions.append(func_skel)

        elif isinstance(node, ast.ClassDef):
            cls_skel = ClassSkeleton(
                name=node.name,
                lineno=node.lineno,
                end_lineno=node.end_lineno or node.lineno,
                bases=[ast.unparse(b) for b in node.bases],
                decorators=_extract_decorators(lines, node),
                docstring=_extract_docstring(node),
                attributes=_extract_class_attributes(node, lines),
            )

            # Extract methods
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_skel = FunctionSkeleton(
                        name=child.name,
                        lineno=child.lineno,
                        end_lineno=child.end_lineno or child.lineno,
                        signature=_extract_signature(lines, child),
                        decorators=_extract_decorators(lines, child),
                        docstring=_extract_docstring(child),
                    )
                    cls_skel.methods.append(method_skel)

            skel.classes.append(cls_skel)

        elif isinstance(node, ast.If):
            # Check for if __name__ == "__main__":
            if (isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"):
                skel.has_main_guard = True

    return skel


def _parse_js_file_to_skeleton(filepath: str) -> Optional[FileSkeleton]:
    """Parse a JS/TS file into a FileSkeleton using regex patterns."""
    try:
        with open(filepath) as f:
            source = f.read()
    except OSError:
        return None

    lines = source.splitlines()
    content_hash = hashlib.md5(source.encode()).hexdigest()

    skel = FileSkeleton(
        path=os.path.basename(filepath),
        total_lines=len(lines),
        source_lines=lines,
        content_hash=content_hash,
    )

    # Patterns for JS/TS constructs
    _import_re = re.compile(r'''^\s*(?:import\s+.*?from\s+['"]|import\s+['"]|(?:const|let|var)\s+.*?=\s*require\s*\()''')
    _export_re = re.compile(r'^\s*export\s+')
    _class_re = re.compile(r'^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)')
    _func_re = re.compile(r'^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[<(]')
    _arrow_re = re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*\S+\s*)?=\s*(?:async\s+)?\(')
    _const_re = re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+([A-Z_][A-Z_0-9]+)\s*=')
    _interface_re = re.compile(r'^\s*(?:export\s+)?(?:interface|type)\s+(\w+)')
    _method_re = re.compile(r'^\s+(?:async\s+)?(?:static\s+)?(\w+)\s*\(')

    i = 0
    current_class = None
    brace_depth = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track brace depth for class scope
        brace_depth += stripped.count('{') - stripped.count('}')
        if current_class and brace_depth <= current_class._base_depth:
            current_class = None

        # Imports
        if _import_re.match(line):
            skel.imports.append(stripped)
            i += 1
            continue

        # Class declarations
        m = _class_re.match(line)
        if m:
            name = m.group(1)
            cls_skel = ClassSkeleton(
                name=name, lineno=i + 1, end_lineno=i + 1,
            )
            cls_skel._base_depth = brace_depth - 1  # track for scope exit
            current_class = cls_skel
            skel.classes.append(cls_skel)
            i += 1
            continue

        # Interface/type declarations (treat like classes)
        m = _interface_re.match(line)
        if m:
            name = m.group(1)
            skel.classes.append(ClassSkeleton(
                name=name, lineno=i + 1, end_lineno=i + 1,
            ))
            i += 1
            continue

        # Function declarations
        m = _func_re.match(line)
        if m:
            name = m.group(1)
            end = _find_block_end(lines, i)
            skel.functions.append(FunctionSkeleton(
                name=name, lineno=i + 1, end_lineno=end + 1,
                signature=stripped,
            ))
            i += 1
            continue

        # Arrow function assignments
        m = _arrow_re.match(line)
        if m:
            name = m.group(1)
            end = _find_block_end(lines, i)
            if current_class:
                current_class.methods.append(FunctionSkeleton(
                    name=name, lineno=i + 1, end_lineno=end + 1,
                    signature=stripped,
                ))
            else:
                skel.functions.append(FunctionSkeleton(
                    name=name, lineno=i + 1, end_lineno=end + 1,
                    signature=stripped,
                ))
            i += 1
            continue

        # Class methods
        if current_class and _method_re.match(line):
            m = _method_re.match(line)
            name = m.group(1)
            if name not in ('if', 'for', 'while', 'switch', 'catch', 'return'):
                end = _find_block_end(lines, i)
                current_class.methods.append(FunctionSkeleton(
                    name=name, lineno=i + 1, end_lineno=end + 1,
                    signature=stripped,
                ))
                current_class.end_lineno = end + 1

        # Constants
        m = _const_re.match(line)
        if m and not current_class:
            skel.constants.append(stripped[:100])

        i += 1

    # Update class end_lineno
    for cls in skel.classes:
        if hasattr(cls, '_base_depth'):
            delattr(cls, '_base_depth')

    return skel


def _find_block_end(lines: list[str], start: int) -> int:
    """Find the end of a brace-delimited block starting at `start`."""
    depth = 0
    for i in range(start, min(start + 200, len(lines))):
        stripped = lines[i].strip()
        depth += stripped.count('{') - stripped.count('}')
        if depth <= 0 and i > start:
            return i
    return min(start + 1, len(lines) - 1)


def _parse_file_regex(filepath: str, lines: list[str], content_hash: str) -> FileSkeleton:
    """Fallback regex-based parsing for files with syntax errors."""
    skel = FileSkeleton(
        path=os.path.basename(filepath),
        total_lines=len(lines),
        source_lines=lines,
        content_hash=content_hash,
    )

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            skel.imports.append(stripped)
        elif re.match(r"^[A-Z_]+\s*=", stripped):
            skel.constants.append(stripped[:100])
        elif re.match(r"^class\s+\w+", stripped):
            name = re.match(r"class\s+(\w+)", stripped).group(1)
            skel.classes.append(ClassSkeleton(
                name=name, lineno=i + 1, end_lineno=i + 1,
            ))
        elif re.match(r"^def\s+\w+", stripped):
            name = re.match(r"def\s+(\w+)", stripped).group(1)
            skel.functions.append(FunctionSkeleton(
                name=name, lineno=i + 1, end_lineno=i + 1,
                signature=stripped,
            ))
        elif stripped.startswith('if __name__'):
            skel.has_main_guard = True

    return skel


# ── Failure parsing ──────────────────────────────────────────────────────────

# Patterns to extract file:function from failure output
_TRACEBACK_RE = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')
_PYTEST_RE = re.compile(r'(\w+\.py)::(\w+)')
_ATTR_ERROR_RE = re.compile(r"'(\w+)' object has no attribute '(\w+)'")
_FLAKE_RE = re.compile(r'(\w+\.py):(\d+):\d+:')


def parse_failures_to_targets(
    failure_text: str,
    skeletons: dict[str, FileSkeleton],
) -> dict[str, set[str]]:
    """Parse pytest/validation failures into {filename: {function_names_to_expand}}.

    Identifies which functions are relevant to the failures and should be
    shown with full source instead of bookends.
    """
    targets: dict[str, set[str]] = {}

    if not failure_text:
        return targets

    def _add(fname: str, func_name: str):
        if fname in skeletons:
            targets.setdefault(fname, set()).add(func_name)

    # Pattern 1: Tracebacks — File "game.py", line 85, in _update_score
    for match in _TRACEBACK_RE.finditer(failure_text):
        fpath, lineno, func_name = match.group(1), int(match.group(2)), match.group(3)
        fname = os.path.basename(fpath)
        _add(fname, func_name)
        # Also find containing function by line number
        if fname in skeletons:
            containing = _find_function_at_line(skeletons[fname], lineno)
            if containing:
                _add(fname, containing)

    # Pattern 2: pytest paths — test_game.py::test_scoring
    for match in _PYTEST_RE.finditer(failure_text):
        fname, func_name = match.group(1), match.group(2)
        _add(fname, func_name)

    # Pattern 3: AttributeError — 'Game' object has no attribute 'foo'
    for match in _ATTR_ERROR_RE.finditer(failure_text):
        class_name = match.group(1)
        # Find __init__ of that class
        for fname, skel in skeletons.items():
            for cls in skel.classes:
                if cls.name == class_name:
                    _add(fname, "__init__")
                    # Also add the missing attribute's potential method
                    attr_name = match.group(2)
                    for method in cls.methods:
                        if method.name == attr_name:
                            _add(fname, attr_name)

    # Pattern 4: Flake8/lint — board.py:46:5: F841
    for match in _FLAKE_RE.finditer(failure_text):
        fname, lineno = match.group(1), int(match.group(2))
        if fname in skeletons:
            containing = _find_function_at_line(skeletons[fname], lineno)
            if containing:
                _add(fname, containing)

    # One-hop transitive expansion: if target function calls other local functions, expand those too
    expanded = {}
    for fname, funcs in targets.items():
        expanded[fname] = set(funcs)
        if fname in skeletons:
            skel = skeletons[fname]
            for func_name in funcs:
                callees = _find_callees(skel, func_name)
                expanded[fname].update(callees)

    return expanded


def _find_function_at_line(skel: FileSkeleton, lineno: int) -> Optional[str]:
    """Find the function containing a given line number."""
    # Check class methods first
    for cls in skel.classes:
        for method in cls.methods:
            if method.lineno <= lineno <= method.end_lineno:
                return method.name
    # Check top-level functions
    for func in skel.functions:
        if func.lineno <= lineno <= func.end_lineno:
            return func.name
    return None


def _find_callees(skel: FileSkeleton, func_name: str) -> set[str]:
    """Find functions called by func_name (one-hop, simple token scan)."""
    callees = set()
    lines = skel.source_lines

    # Find the function body
    target = None
    all_funcs = list(skel.functions)
    for cls in skel.classes:
        all_funcs.extend(cls.methods)

    func_names = {f.name for f in all_funcs}

    for func in all_funcs:
        if func.name == func_name:
            target = func
            break

    if not target:
        return callees

    # Scan body for calls to other local functions
    body = "\n".join(lines[target.lineno - 1:target.end_lineno])
    for other_name in func_names:
        if other_name != func_name and f"{other_name}(" in body:
            callees.add(other_name)
        # Also check self.method() pattern
        if other_name != func_name and f"self.{other_name}(" in body:
            callees.add(other_name)

    return callees


# ── Skeleton rendering — bookend style ───────────────────────────────────────

def _render_bookend(
    lines: list[str],
    start: int,
    end: int,
    indent: str = "",
) -> list[str]:
    """Render a function body with bookend style (first/last lines, skip middle).

    Args:
        lines: 0-indexed source lines
        start: 1-indexed first line
        end: 1-indexed last line
        indent: prefix for indentation
    """
    n = end - start + 1
    parts = []

    if n <= 6:
        # Show full body
        for i in range(start - 1, end):
            if i < len(lines):
                parts.append(f"{i + 1:4d}| {lines[i]}")
    elif n <= 15:
        # First 3 + last 1
        for i in range(start - 1, min(start + 2, end)):
            if i < len(lines):
                parts.append(f"{i + 1:4d}| {lines[i]}")
        skipped = n - 4
        parts.append(f"     {indent}... ({skipped} lines) ...")
        if end - 1 < len(lines):
            parts.append(f"{end:4d}| {lines[end - 1]}")
    else:
        # First 3 + last 2
        for i in range(start - 1, min(start + 2, end)):
            if i < len(lines):
                parts.append(f"{i + 1:4d}| {lines[i]}")
        skipped = n - 5
        parts.append(f"     {indent}... ({skipped} lines) ...")
        for i in range(max(end - 2, start + 2), end):
            if i < len(lines):
                parts.append(f"{i + 1:4d}| {lines[i]}")

    return parts


def _render_full(lines: list[str], start: int, end: int) -> list[str]:
    """Render full source with line numbers."""
    parts = []
    for i in range(start - 1, min(end, len(lines))):
        parts.append(f"{i + 1:4d}| {lines[i]}")
    return parts


def render_file_skeleton(
    skel: FileSkeleton,
    expand_functions: Optional[set[str]] = None,
    compact: bool = False,
) -> str:
    """Render a file skeleton with bookend-style functions.

    Args:
        skel: The file skeleton to render
        expand_functions: Set of function names to show with full source
        compact: If True, skip docstrings and attributes (T4 minimal mode)
    """
    expand = expand_functions or set()
    lines = skel.source_lines
    parts = []

    # Header
    expanded_names = [n for n in expand if n in _all_func_names(skel)]
    if expanded_names:
        parts.append(f"### {skel.path} ({skel.total_lines} lines) [EXPANDED: {', '.join(sorted(expanded_names))}]")
    else:
        parts.append(f"### {skel.path} ({skel.total_lines} lines)")

    # Module docstring
    if skel.docstring and not compact:
        parts.append(f"   1| \"\"\"{skel.docstring}\"\"\"")

    # Imports (always verbatim)
    if skel.imports:
        for imp in skel.imports:
            # Find the line number
            for i, line in enumerate(lines):
                if line.strip() == imp:
                    parts.append(f"{i + 1:4d}| {line}")
                    break

    # Constants — always included (cross-module values like PAGE_SIZE=4096 are
    # contract glue; drift between writer's 4096 and reader's 8192 is invisible
    # without these. Cap at 10 to bound prompt size.
    if skel.constants:
        for const in skel.constants[:10]:
            for i, line in enumerate(lines):
                if line.strip().startswith(const.split("=")[0].strip()):
                    parts.append(f"{i + 1:4d}| {line}")
                    break

    # Top-level functions
    for func in skel.functions:
        parts.append("")
        if func.name in expand:
            parts.append("## ── EXPANDED (failure-relevant) ──")
            parts.extend(_render_full(lines, func.lineno, func.end_lineno))
            parts.append("## ── END EXPANDED ──")
        else:
            # Decorators
            for deco in func.decorators:
                for i, line in enumerate(lines):
                    if line.strip() == deco and func.lineno - 5 <= i + 1 <= func.lineno:
                        parts.append(f"{i + 1:4d}| {line}")
                        break
            parts.extend(_render_bookend(lines, func.lineno, func.end_lineno))

    # Classes
    for cls in skel.classes:
        parts.append("")
        # Class definition line
        if cls.lineno - 1 < len(lines):
            # Decorators
            for deco in cls.decorators:
                for i, line in enumerate(lines):
                    if line.strip() == deco and cls.lineno - 5 <= i + 1 <= cls.lineno:
                        parts.append(f"{i + 1:4d}| {line}")
                        break
            parts.append(f"{cls.lineno:4d}| {lines[cls.lineno - 1]}")

        # Class docstring
        if cls.docstring and not compact:
            # Find docstring line
            for i in range(cls.lineno, min(cls.lineno + 3, len(lines))):
                if '"""' in lines[i] or "'''" in lines[i]:
                    parts.append(f"{i + 1:4d}| {lines[i]}")
                    break

        # Class attributes — always included (enum values and class constants
        # are load-bearing for cross-module contracts: `ColumnType.INTEGER` vs
        # `.INT` drift is invisible without these lines). Cheap: <50 tokens.
        for attr in cls.attributes:
            for i, line in enumerate(lines):
                if (line.strip() == attr
                        and cls.lineno <= i + 1 <= cls.end_lineno):
                    parts.append(f"{i + 1:4d}| {line}")
                    break

        # Methods — __init__ always rendered with its FULL signature so
        # callers see arg count + type hints without re-reading the file.
        # This is load-bearing for cross-module constructor calls: the LLM
        # at a call site `HeapFile(path, layout)` must see `def __init__(
        # self, path: str, layout: Layout)` not just the first 2 lines.
        for method in cls.methods:
            parts.append("")
            if method.name in expand:
                parts.append("  ## ── EXPANDED (failure-relevant) ──")
                parts.extend(_render_full(lines, method.lineno, method.end_lineno))
                parts.append("  ## ── END EXPANDED ──")
            elif method.name == "__init__":
                # Signature line (may wrap) + body bookend so long __init__
                # with many setattrs still shows contract at a glance.
                sig_end = method.lineno
                # If signature wraps, find the closing paren + colon
                for k in range(method.lineno - 1, min(method.lineno + 10, len(lines))):
                    if lines[k].rstrip().endswith(":") and "def __init__" in "\n".join(
                        lines[method.lineno - 1:k + 1]
                    ):
                        sig_end = k + 1
                        break
                for i in range(method.lineno - 1, sig_end):
                    parts.append(f"{i + 1:4d}| {lines[i]}")
                # Add a brief body sample (2 lines) so behavior hint is there
                body_preview_end = min(sig_end + 2, method.end_lineno)
                for i in range(sig_end, body_preview_end):
                    parts.append(f"{i + 1:4d}| {lines[i]}")
                if body_preview_end < method.end_lineno - 1:
                    parts.append(f"       ... ({method.end_lineno - body_preview_end} more lines) ...")
            else:
                # Decorators
                for deco in method.decorators:
                    for i, line in enumerate(lines):
                        if line.strip() == deco and method.lineno - 5 <= i + 1 <= method.lineno:
                            parts.append(f"{i + 1:4d}| {line}")
                            break
                parts.extend(_render_bookend(lines, method.lineno, method.end_lineno))

    # Main guard
    if skel.has_main_guard:
        for i, line in enumerate(lines):
            if line.strip().startswith("if __name__"):
                parts.append("")
                parts.extend(_render_bookend(lines, i + 1, skel.total_lines))
                break

    return "\n".join(parts)


def _all_func_names(skel: FileSkeleton) -> set[str]:
    """Get all function/method names in a skeleton."""
    names = {f.name for f in skel.functions}
    for cls in skel.classes:
        names.update(m.name for m in cls.methods)
    return names


# ── Budget tiers ─────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """Rough token estimate (1 token ≈ 4 chars)."""
    return len(text) // 4


def select_tier(
    full_tokens: int,
    skeleton_tokens: int,
    test_tokens: int,
    budget: int,
    has_failures: bool,
) -> int:
    """Select rendering tier based on token budget.

    Returns tier number 1-5.
    """
    if full_tokens < budget * 0.6:
        return 1  # Full source for everything
    if skeleton_tokens + test_tokens < budget and not has_failures:
        return 2  # Skeleton all + full tests
    if skeleton_tokens + test_tokens < budget * 0.6:
        return 3  # Skeleton + expanded failures + full tests
    if skeleton_tokens < budget * 0.4:
        return 4  # Compact skeleton + only failing functions + tests
    return 5  # Only files mentioned in failures


# ── CodeMapBuilder ───────────────────────────────────────────────────────────

# Agent guide injected into the code map
AGENT_CODEMAP_GUIDE_FULL = """\
## Code Map

Complete source code for all project files with real line numbers.
Use edit_file with exact string matching from the source below.
Use line_edit(path, start, end, new_code) to replace lines by number.
"""

AGENT_CODEMAP_GUIDE_SKELETON = """\
## How the Code Map Works

The code map below shows the entire project structure with real line numbers.

### Detail levels:

1. BOOKEND — Most functions. Shows opening 2-3 lines + closing 1-2 lines:
      46|     def clear_lines(self) -> int:
      47|         \"\"\"Clear completed lines and return count.\"\"\"
      48|         lines_cleared = 0
               ... (13 lines) ...
      62|         return lines_cleared

   Use bookend lines as anchors for edit_file, or line_edit(path, 46, 62, new_code).

2. EXPANDED — Failure-relevant functions shown in full between ## EXPANDED markers.

3. FULL — Test files shown with complete source.

Line numbers are AUTHORITATIVE. After edits, the code map refreshes with updated numbers.
"""


class CodeMapBuilder:
    """Builds budget-aware code maps with caching and incremental refresh."""

    def __init__(self, workspace: str, budget_tokens: int = 30000, lang=None):
        self.workspace = workspace
        self.budget_tokens = budget_tokens
        self.lang = lang
        self._extensions = tuple(lang.extensions) if lang else (".py",)
        self._skip_dirs = {"__pycache__", ".venv", ".cadillac"}
        if lang and lang.family == "node":
            self._skip_dirs.add("node_modules")
        self._cache: dict[str, FileSkeleton] = {}  # fname -> skeleton
        self._hash_cache: dict[str, str] = {}  # fname -> content_hash
        self.tier: int = 1
        self.last_tokens: int = 0

    def _discover_files(self) -> list[str]:
        """Find all source files in workspace."""
        src_files = []
        for root, _, files in os.walk(self.workspace):
            if any(sd in root for sd in self._skip_dirs):
                continue
            for fname in sorted(files):
                if fname.endswith(self._extensions) and not fname.startswith("."):
                    src_files.append(os.path.relpath(
                        os.path.join(root, fname), self.workspace
                    ))
        return sorted(src_files)

    def _parse_all(self, force_files: Optional[set[str]] = None) -> dict[str, FileSkeleton]:
        """Parse all .py files, using cache for unchanged files."""
        py_files = self._discover_files()
        skeletons = {}

        for fname in py_files:
            fpath = os.path.join(self.workspace, fname)

            # Check cache
            if fname in self._cache and fname not in (force_files or set()):
                try:
                    with open(fpath) as f:
                        content = f.read()
                    current_hash = hashlib.md5(content.encode()).hexdigest()
                    if current_hash == self._hash_cache.get(fname):
                        skeletons[fname] = self._cache[fname]
                        continue
                except OSError:
                    pass

            skel = parse_file_to_skeleton(fpath)
            if skel:
                skel.path = fname
                skeletons[fname] = skel
                self._cache[fname] = skel
                self._hash_cache[fname] = skel.content_hash

        return skeletons

    def _is_test_file(self, fname: str) -> bool:
        """Check if a file is a test file."""
        base = os.path.basename(fname)
        return (base.startswith("test_") or base.endswith("_test.py")
                or ".test." in base or ".spec." in base)

    def _render_full_source(self, skel: FileSkeleton) -> str:
        """Render full source with line numbers."""
        parts = [f"### {skel.path} ({skel.total_lines} lines) [FULL]"]
        for i, line in enumerate(skel.source_lines):
            parts.append(f"{i + 1:4d}| {line}")
        return "\n".join(parts)

    def build(self, failure_text: str = "") -> str:
        """Build the complete code map.

        Args:
            failure_text: pytest/validation failure output for targeted expansion
        """
        skeletons = self._parse_all()

        if not skeletons:
            return "## CODE MAP\n(no source files found)"

        # Parse failures to targets
        targets = parse_failures_to_targets(failure_text, skeletons) if failure_text else {}

        # Calculate token costs for tier selection
        full_parts = []
        for fname, skel in sorted(skeletons.items()):
            full_parts.append(self._render_full_source(skel))
        full_text = "\n\n".join(full_parts)
        full_tokens = _estimate_tokens(full_text)

        skel_parts = []
        test_parts = []
        for fname, skel in sorted(skeletons.items()):
            if self._is_test_file(fname):
                test_parts.append(self._render_full_source(skel))
            else:
                skel_parts.append(render_file_skeleton(skel))
        skeleton_tokens = _estimate_tokens("\n\n".join(skel_parts))
        test_tokens = _estimate_tokens("\n\n".join(test_parts))

        # Select tier
        self.tier = select_tier(
            full_tokens, skeleton_tokens, test_tokens,
            self.budget_tokens, bool(targets),
        )

        # Render based on tier
        rendered = self._render_tier(skeletons, targets)
        self.last_tokens = _estimate_tokens(rendered)
        self._last_output = rendered
        return rendered

    def rebuild(self, changed_files: Optional[set[str]] = None, failure_text: str = "") -> str:
        """Rebuild code map, only re-parsing changed files."""
        if changed_files:
            # Force re-parse of changed files
            for fname in changed_files:
                self._hash_cache.pop(fname, None)
                self._cache.pop(fname, None)

        return self.build(failure_text=failure_text)

    def _render_tier(self, skeletons: dict[str, FileSkeleton], targets: dict[str, set[str]]) -> str:
        """Render code map based on selected tier."""
        guide = AGENT_CODEMAP_GUIDE_FULL if self.tier == 1 else AGENT_CODEMAP_GUIDE_SKELETON
        parts = [guide, f"## CODE MAP (Tier {self.tier})\n"]

        if self.tier == 1:
            # T1: Full source for everything
            for fname in sorted(skeletons):
                parts.append(self._render_full_source(skeletons[fname]))
                parts.append("")

        elif self.tier == 2:
            # T2: Skeleton all + full tests
            for fname in sorted(skeletons):
                if self._is_test_file(fname):
                    parts.append(self._render_full_source(skeletons[fname]))
                else:
                    parts.append(render_file_skeleton(skeletons[fname]))
                parts.append("")

        elif self.tier == 3:
            # T3: Skeleton + expanded failures + full tests
            for fname in sorted(skeletons):
                if self._is_test_file(fname):
                    parts.append(self._render_full_source(skeletons[fname]))
                else:
                    expand = targets.get(fname, set())
                    parts.append(render_file_skeleton(skeletons[fname], expand_functions=expand))
                parts.append("")

        elif self.tier == 4:
            # T4: Compact skeleton + only failing functions + tests
            for fname in sorted(skeletons):
                if self._is_test_file(fname):
                    parts.append(self._render_full_source(skeletons[fname]))
                else:
                    expand = targets.get(fname, set())
                    parts.append(render_file_skeleton(
                        skeletons[fname], expand_functions=expand, compact=True,
                    ))
                parts.append("")

        elif self.tier == 5:
            # T5: Only files mentioned in failures + test files
            relevant_files = set(targets.keys())
            for fname in sorted(skeletons):
                if self._is_test_file(fname):
                    parts.append(self._render_full_source(skeletons[fname]))
                    parts.append("")
                elif fname in relevant_files:
                    expand = targets.get(fname, set())
                    parts.append(render_file_skeleton(
                        skeletons[fname], expand_functions=expand, compact=True,
                    ))
                    parts.append("")
                else:
                    # Just a one-liner summary
                    skel = skeletons[fname]
                    class_names = [c.name for c in skel.classes]
                    func_names = [f.name for f in skel.functions]
                    summary = f"### {fname} ({skel.total_lines} lines)"
                    if class_names:
                        summary += f" — classes: {', '.join(class_names)}"
                    if func_names:
                        summary += f" — functions: {', '.join(func_names)}"
                    parts.append(summary)

        return "\n".join(parts)


class ModuleCodeMapBuilder(CodeMapBuilder):
    """CodeMapBuilder scoped to a single module's directory.

    Only discovers source files under module_path. With ~10 files per module,
    this always stays at tier 1 or 2 (full source or skeleton).
    """

    def __init__(self, workspace: str, module_path: str, budget_tokens: int = 20000, lang=None):
        super().__init__(workspace, budget_tokens=budget_tokens, lang=lang)
        self.module_path = module_path.rstrip("/")

    def _discover_files(self) -> list[str]:
        """Find source files only under the module directory."""
        module_dir = os.path.join(self.workspace, self.module_path)
        src_files = []
        if not os.path.isdir(module_dir):
            return src_files
        for root, _, files in os.walk(module_dir):
            if any(sd in root for sd in self._skip_dirs):
                continue
            for fname in sorted(files):
                if fname.endswith(self._extensions) and not fname.startswith("."):
                    src_files.append(os.path.relpath(
                        os.path.join(root, fname), self.workspace
                    ))
        return sorted(src_files)
