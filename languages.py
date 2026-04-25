"""Language strategy — pluggable language support for Python, TypeScript, React, Vue, Angular, and HTML/CSS/JS."""

import os
import re
from dataclasses import dataclass, field
from typing import Callable

from . import quality


@dataclass
class Language:
    """Configuration for a target language, plugged into the build pipeline."""
    name: str                      # "python" | "typescript" | "react" | "go" | "c"
    family: str                    # "python" | "node" | "compiled" | "static"
    extensions: list[str]          # [".py"] | [".ts", ".tsx", ".js", ".jsx"]
    entry_point: str               # "main.py" | "src/index.ts"
    init_file: str | None          # "__init__.py" | "index.ts" | None
    test_prefix: str               # "test_" | ""
    test_suffix: str               # "" | ".test"
    package_file: str | None       # None | "package.json"
    install_cmd: str               # "pip3 install" | "npm install"
    run_cmd: str                   # "python3" | "npx ts-node"
    build_cmd: str = ""            # "" | "npx vite build" | "make" | "go build"
    test_cmd: list[str] = field(default_factory=list)
    lint_cmd: list[str] | None = None
    syntax_check_cmd: list[str] = field(default_factory=list)
    stdlib_modules: set[str] = field(default_factory=set)
    import_pattern: re.Pattern | None = None
    fence_langs: list[str] = field(default_factory=list)
    # Quality content wired from quality.py
    coding_standards: str = ""
    project_structure: str = ""
    anti_patterns: str = ""
    few_shot_scaffold: str = ""
    few_shot_main: str = ""
    few_shot_db_pattern: str = ""
    functional_test_guidance: str = ""
    few_shot_naming: str = ""
    iterate_prompt: str = ""
    iterate_feature_prompt: str = ""


# ── Python stdlib module names (for shadow detection) ─────────────────────────

_PYTHON_STDLIB = {
    "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio", "asyncore",
    "atexit", "audioop", "base64", "bdb", "binascii", "binhex", "bisect",
    "builtins", "bz2", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd",
    "code", "codecs", "codeop", "collections", "colorsys", "compileall",
    "concurrent", "configparser", "contextlib", "contextvars", "copy", "copyreg",
    "cProfile", "crypt", "csv", "ctypes", "curses", "dataclasses", "datetime",
    "dbm", "decimal", "difflib", "dis", "distutils", "doctest", "email",
    "encodings", "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
    "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt", "getpass",
    "gettext", "glob", "grp", "gzip", "hashlib", "heapq", "hmac", "html",
    "http", "idlelib", "imaplib", "imghdr", "imp", "importlib", "inspect",
    "io", "ipaddress", "itertools", "json", "keyword", "lib2to3", "linecache",
    "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math",
    "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc", "nis",
    "nntplib", "numbers", "operator", "optparse", "os", "ossaudiodev",
    "pathlib", "parser", "pdb", "pickle", "pickletools", "pipes", "pkgutil",
    "platform", "plistlib", "poplib", "posix", "posixpath", "pprint",
    "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc",
    "queue", "quopri", "random", "re", "readline", "reprlib", "resource",
    "rlcompleter", "runpy", "sched", "secrets", "select", "selectors",
    "shelve", "shlex", "shutil", "signal", "site", "smtpd", "smtplib",
    "sndhdr", "socket", "socketserver", "sqlite3", "ssl", "stat", "statistics",
    "string", "stringprep", "struct", "subprocess", "sunau", "symtable", "sys",
    "sysconfig", "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile",
    "termios", "test", "textwrap", "threading", "time", "timeit", "tkinter",
    "token", "tokenize", "tomllib", "trace", "traceback", "tracemalloc",
    "tty", "turtle", "turtledemo", "types", "typing", "unicodedata",
    "unittest", "urllib", "uu", "uuid", "venv", "warnings", "wave",
    "weakref", "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib",
    "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
}

# ── Node.js built-in module names ─────────────────────────────────────────────

_NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers",
    "tls", "trace_events", "tty", "url", "util", "v8", "vm",
    "wasi", "worker_threads", "zlib",
}

# Browser globals that shouldn't trigger naming conflicts
_BROWSER_GLOBALS = {
    "document", "window", "navigator", "location", "history", "screen",
    "console", "fetch", "localStorage", "sessionStorage", "crypto",
    "performance", "url", "event", "error",
}


def python_language() -> Language:
    """Create Language config for Python projects."""
    return Language(
        name="python",
        family="python",
        extensions=[".py"],
        entry_point="main.py",
        init_file="__init__.py",
        test_prefix="test_",
        test_suffix="",
        package_file=None,
        install_cmd="pip3 install",
        run_cmd="python3",
        test_cmd=["python3", "-m", "pytest", "-x", "--tb=short", "-q"],
        lint_cmd=["ruff", "check", "--select=E,F,W"],
        syntax_check_cmd=["python3", "-m", "py_compile"],
        stdlib_modules=_PYTHON_STDLIB,
        import_pattern=re.compile(r'^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))'),
        fence_langs=["python"],
        coding_standards=quality.CODING_STANDARDS,
        project_structure=quality.PROJECT_STRUCTURE,
        anti_patterns=quality.ANTI_PATTERNS,
        few_shot_scaffold=quality.FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.FEW_SHOT_MAIN_PY,
        few_shot_db_pattern=quality.FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.FEW_SHOT_NAMING,
        iterate_prompt=quality.ITERATE_PROMPT,
        iterate_feature_prompt=quality.ITERATE_FEATURE_PROMPT,
    )


def typescript_language() -> Language:
    """Create Language config for TypeScript/JavaScript projects."""
    return Language(
        name="typescript",
        family="node",
        extensions=[".ts", ".tsx", ".js", ".jsx"],
        entry_point="src/index.ts",
        init_file="index.ts",
        test_prefix="",
        test_suffix=".test",
        package_file="package.json",
        install_cmd="npm install",
        run_cmd="npx ts-node",
        test_cmd=["npx", "jest", "--passWithNoTests"],
        lint_cmd=["npx", "eslint", "."],
        syntax_check_cmd=["npx", "tsc", "--noEmit"],
        stdlib_modules=_NODE_BUILTINS,
        import_pattern=re.compile(
            r'''^\s*(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]'''
            r'''|import\s+['"]([^'"]+)['"]'''
            r'''|(?:const|let|var)\s+.*?=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\))'''
        ),
        fence_langs=["typescript", "javascript"],
        coding_standards=quality.TS_CODING_STANDARDS,
        project_structure=quality.TS_PROJECT_STRUCTURE,
        anti_patterns=quality.TS_ANTI_PATTERNS,
        few_shot_scaffold=quality.TS_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.TS_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.TS_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.TS_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.TS_FEW_SHOT_NAMING,
        iterate_prompt=quality.TS_ITERATE_PROMPT,
        iterate_feature_prompt=quality.TS_ITERATE_FEATURE_PROMPT,
    )


def html_language() -> Language:
    """Create Language config for static HTML/CSS/JavaScript projects (no frameworks, no npm)."""
    return Language(
        name="html",
        family="static",
        extensions=[".html", ".css", ".js"],
        entry_point="index.html",
        init_file=None,
        test_prefix="test_",
        test_suffix="",
        package_file=None,
        install_cmd="",
        run_cmd="node",
        test_cmd=["node", "test.js"],
        lint_cmd=None,
        syntax_check_cmd=["node", "--check"],
        stdlib_modules=_BROWSER_GLOBALS,
        import_pattern=re.compile(r'^\s*import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]'),
        fence_langs=["html", "javascript", "css"],
        coding_standards=quality.HTML_CODING_STANDARDS,
        project_structure=quality.HTML_PROJECT_STRUCTURE,
        anti_patterns=quality.HTML_ANTI_PATTERNS,
        few_shot_scaffold=quality.HTML_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.HTML_FEW_SHOT_MAIN,
        few_shot_db_pattern="",
        functional_test_guidance=quality.HTML_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.HTML_FEW_SHOT_NAMING,
        iterate_prompt=quality.HTML_ITERATE_PROMPT,
        iterate_feature_prompt=quality.HTML_ITERATE_FEATURE_PROMPT,
    )


def react_language() -> Language:
    """Create Language config for React + TypeScript projects (Vite)."""
    return Language(
        name="react",
        family="node",
        extensions=[".tsx", ".ts", ".jsx", ".js", ".css"],
        entry_point="src/main.tsx",
        init_file="index.ts",
        test_prefix="",
        test_suffix=".test",
        package_file="package.json",
        install_cmd="npm install",
        run_cmd="npx vite preview",
        build_cmd="npx vite build",
        test_cmd=["npx", "vitest", "run"],
        lint_cmd=["npx", "eslint", "."],
        syntax_check_cmd=["npx", "tsc", "--noEmit"],
        stdlib_modules=_NODE_BUILTINS,
        import_pattern=re.compile(
            r'''^\s*(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]'''
            r'''|import\s+['"]([^'"]+)['"]'''
            r'''|(?:const|let|var)\s+.*?=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\))'''
        ),
        fence_langs=["typescript", "tsx", "javascript"],
        coding_standards=quality.REACT_CODING_STANDARDS,
        project_structure=quality.REACT_PROJECT_STRUCTURE,
        anti_patterns=quality.REACT_ANTI_PATTERNS,
        few_shot_scaffold=quality.REACT_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.REACT_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.REACT_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.REACT_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.REACT_FEW_SHOT_NAMING,
        iterate_prompt=quality.REACT_ITERATE_PROMPT,
        iterate_feature_prompt=quality.REACT_ITERATE_FEATURE_PROMPT,
    )


def vue_language() -> Language:
    """Create Language config for Vue 3 + TypeScript projects (Vite)."""
    return Language(
        name="vue",
        family="node",
        extensions=[".vue", ".ts", ".js", ".css"],
        entry_point="src/main.ts",
        init_file="index.ts",
        test_prefix="",
        test_suffix=".test",
        package_file="package.json",
        install_cmd="npm install",
        run_cmd="npx vite preview",
        build_cmd="npx vite build",
        test_cmd=["npx", "vitest", "run"],
        lint_cmd=["npx", "eslint", "."],
        syntax_check_cmd=["npx", "tsc", "--noEmit"],
        stdlib_modules=_NODE_BUILTINS,
        import_pattern=re.compile(
            r'''^\s*(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]'''
            r'''|import\s+['"]([^'"]+)['"]'''
            r'''|(?:const|let|var)\s+.*?=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\))'''
        ),
        fence_langs=["typescript", "vue", "javascript"],
        coding_standards=quality.VUE_CODING_STANDARDS,
        project_structure=quality.VUE_PROJECT_STRUCTURE,
        anti_patterns=quality.VUE_ANTI_PATTERNS,
        few_shot_scaffold=quality.VUE_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.VUE_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.VUE_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.VUE_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.VUE_FEW_SHOT_NAMING,
        iterate_prompt=quality.VUE_ITERATE_PROMPT,
        iterate_feature_prompt=quality.VUE_ITERATE_FEATURE_PROMPT,
    )


def angular_language() -> Language:
    """Create Language config for Angular 17+ projects."""
    return Language(
        name="angular",
        family="node",
        extensions=[".ts", ".html", ".css", ".scss"],
        entry_point="src/main.ts",
        init_file="index.ts",
        test_prefix="",
        test_suffix=".spec",
        package_file="package.json",
        install_cmd="npm install",
        run_cmd="npx ng serve",
        build_cmd="npx ng build",
        test_cmd=["npx", "ng", "test", "--watch=false", "--browsers=ChromeHeadless"],
        lint_cmd=["npx", "eslint", "."],
        syntax_check_cmd=["npx", "tsc", "--noEmit"],
        stdlib_modules=_NODE_BUILTINS,
        import_pattern=re.compile(
            r'''^\s*(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]'''
            r'''|import\s+['"]([^'"]+)['"]'''
            r'''|(?:const|let|var)\s+.*?=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\))'''
        ),
        fence_langs=["typescript", "html"],
        coding_standards=quality.ANGULAR_CODING_STANDARDS,
        project_structure=quality.ANGULAR_PROJECT_STRUCTURE,
        anti_patterns=quality.ANGULAR_ANTI_PATTERNS,
        few_shot_scaffold=quality.ANGULAR_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.ANGULAR_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.ANGULAR_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.ANGULAR_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.ANGULAR_FEW_SHOT_NAMING,
        iterate_prompt=quality.ANGULAR_ITERATE_PROMPT,
        iterate_feature_prompt=quality.ANGULAR_ITERATE_FEATURE_PROMPT,
    )


# ── Language detection ────────────────────────────────────────────────────────

# Signals that the user wants plain HTML/CSS/JS (no frameworks, no npm)
_HTML_INDICATORS = frozenset({
    "plain html", "vanilla javascript", "vanilla js", "static site",
    "static website", "html/css/js", "html, css, and", "html, javascript",
    "html and javascript", "html css javascript", "no framework",
    "plain javascript", "plain js", "pure html", "pure javascript",
    "pure js", "static html", "html, css and", "html and css",
    "in html", "using html",
})

# Framework/tooling keywords that override HTML detection → TypeScript
_FRAMEWORK_KEYWORDS = frozenset({
    "react", "next.js", "nextjs", "express", "typescript",
    "vue", "svelte", "angular", "electron",
    "nest", "nestjs", "fastify", "koa", "webpack", "vite",
})

_JS_KEYWORDS = frozenset({
    "react", "next.js", "nextjs", "express", "node", "typescript",
    "javascript", "npm", "vue", "svelte", "angular", "electron",
    "webpack", "vite", "tailwind", "html", "css", "web app",
    "frontend", "full-stack", "fullstack", "rest api", "graphql",
    "nest", "nestjs", "fastify", "koa", "deno", "bun",
})

# Python-specific keywords that override JS detection (Flask is Python, not Node)
_PYTHON_KEYWORDS = frozenset({
    "flask", "django", "fastapi", "uvicorn", "gunicorn", "celery",
    "sqlalchemy", "aiosqlite", "asyncio", "pytest", "unittest",
    "pip", "python", "pydantic", "click", "typer", "rich",
    "pygame", "curses", "tkinter", "pyqt", "kivy",
    "pandas", "numpy", "scipy", "torch", "tensorflow",
    ".py", "main.py", "requirements.txt",
})


def detect_language(task: str, workspace: str | None = None) -> Language:
    """Detect target language from task description or existing workspace."""
    task_lower = task.lower()

    # Plain HTML/CSS/JS — check BEFORE TS keywords (which include "html", "css", etc.)
    # But Python keywords override HTML (e.g., "Flask + vanilla HTML" = Python)
    if any(kw in task_lower for kw in _HTML_INDICATORS):
        if not any(kw in task_lower for kw in _FRAMEWORK_KEYWORDS):
            if not any(kw in task_lower for kw in _PYTHON_KEYWORDS):
                return html_language()

    # Framework-specific detection — check BEFORE generic JS/TS
    if any(kw in task_lower for kw in ("react", "nextjs", "next.js")):
        return react_language()
    if "vue" in task_lower:
        return vue_language()
    if "angular" in task_lower:
        return angular_language()

    # Python-specific keywords — check BEFORE JS keywords to prevent
    # false positives (e.g., "Flask REST API" should be Python, not TS)
    if any(kw in task_lower for kw in _PYTHON_KEYWORDS):
        return python_language()

    if any(kw in task_lower for kw in _JS_KEYWORDS):
        return typescript_language()
    if workspace and os.path.exists(os.path.join(workspace, "package.json")):
        return typescript_language()
    return python_language()
