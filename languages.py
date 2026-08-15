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


_GO_STDLIB = {
    # The standard library is large; this set is for shadow/conflict
    # detection. Listing the most likely shadowed names is enough — full
    # stdlib coverage isn't needed and would add noise.
    "fmt", "os", "io", "errors", "log", "time", "sync", "context",
    "bytes", "strings", "strconv", "sort", "encoding", "regexp",
    "bufio", "net", "http", "url", "path", "filepath", "math",
    "reflect", "runtime", "unsafe", "syscall", "testing",
}

_RUST_STDLIB = {
    # Same purpose as _GO_STDLIB — names a generated module/struct must NOT
    # shadow. std + alloc + core paths in common use.
    "std", "core", "alloc", "vec", "string", "result", "option",
    "iter", "collections", "io", "fs", "net", "sync", "thread",
    "time", "fmt", "error", "convert", "ops", "cmp", "boxed",
}


def pytorch_language() -> Language:
    """Create Language config for PyTorch / GPU-ML projects.

    Family is "python" — same toolchain as python_language(). The only
    difference is the prompt content: device discipline (.to(device)),
    training-loop hygiene (zero_grad/backward/step order), the
    overfit-single-batch smoke test, mixed-precision patterns, etc. The
    autobuilder's machine has a 4090; CUDA is available; this strategy
    encodes the patterns that make GPU training work and the patterns
    that silently break it.
    """
    return Language(
        name="pytorch",
        family="python",
        extensions=[".py"],
        entry_point="train.py",
        init_file="__init__.py",
        test_prefix="test_",
        test_suffix="",
        package_file=None,
        install_cmd="pip3 install",
        run_cmd="python3",
        build_cmd="",
        test_cmd=["python3", "-m", "pytest", "-x", "--tb=short", "-q"],
        lint_cmd=None,
        syntax_check_cmd=["python3", "-m", "py_compile"],
        stdlib_modules=_PYTHON_STDLIB,
        import_pattern=re.compile(r'^\s*(?:import\s+(\S+)|from\s+(\S+)\s+import)'),
        fence_langs=["python"],
        coding_standards=quality.PYTORCH_CODING_STANDARDS,
        project_structure=quality.PYTORCH_PROJECT_STRUCTURE,
        anti_patterns=quality.PYTORCH_ANTI_PATTERNS,
        few_shot_scaffold=quality.PYTORCH_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.PYTORCH_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.PYTORCH_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.PYTORCH_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.PYTORCH_FEW_SHOT_NAMING,
        iterate_prompt=quality.PYTORCH_ITERATE_PROMPT,
        iterate_feature_prompt=quality.PYTORCH_ITERATE_FEATURE_PROMPT,
    )


def wordpress_language() -> Language:
    """Create Language config for WordPress plugins (PHP).

    Family is "php" — a new family. Validation hooks (check_syntax,
    check_security) recognize the family and dispatch a php-aware code
    path. WP plugins can't be RUN without a real WordPress install, so
    the entry-point check just verifies the main plugin file exists.
    """
    return Language(
        name="wordpress",
        family="php",
        extensions=[".php"],
        # The plugin's main file is named after the slug. We can't know
        # the slug at construction time, so use a generic placeholder; the
        # real entry is whichever .php at workspace root carries the
        # WP "Plugin Name:" header.
        entry_point="plugin.php",
        init_file=None,
        test_prefix="",
        test_suffix="Test",  # PHPUnit convention: ClassName + "Test"
        package_file=None,   # composer.json optional, not required
        install_cmd="",      # WP plugins install by file copy
        run_cmd="",          # can't run without a WP host
        build_cmd="",        # no build step
        test_cmd=["phpunit"],   # run iff phpunit is installed; harmless skip otherwise
        lint_cmd=None,
        syntax_check_cmd=["php", "-l"],
        stdlib_modules=set(),  # PHP doesn't have a Python-style stdlib namespace
        import_pattern=re.compile(
            r'^\s*(?:require|require_once|include|include_once|use)\s+([^\s;]+)'
        ),
        fence_langs=["php"],
        coding_standards=quality.WORDPRESS_CODING_STANDARDS,
        project_structure=quality.WORDPRESS_PROJECT_STRUCTURE,
        anti_patterns=quality.WORDPRESS_ANTI_PATTERNS,
        few_shot_scaffold=quality.WORDPRESS_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.WORDPRESS_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.WORDPRESS_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.WORDPRESS_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.WORDPRESS_FEW_SHOT_NAMING,
        iterate_prompt=quality.WORDPRESS_ITERATE_PROMPT,
        iterate_feature_prompt=quality.WORDPRESS_ITERATE_FEATURE_PROMPT,
    )


def browser_extension_language() -> Language:
    """Create Language config for Chrome MV3 browser extensions (TS + Vite).

    Same node toolchain as react_language() — same vite/vitest/tsc/eslint —
    plus an MV3 manifest, service-worker background, content scripts, and
    optionally a React popup. Extension can't actually be loaded into
    Chrome here (we're headless), so smoke/run skip; we validate the bundle
    compiles and unit tests pass.
    """
    return Language(
        name="browser_extension",
        family="node",
        extensions=[".ts", ".tsx", ".js", ".jsx", ".json", ".html", ".css"],
        entry_point="manifest.json",
        init_file="index.ts",
        test_prefix="",
        test_suffix=".test",
        package_file="package.json",
        install_cmd="npm install",
        run_cmd="",            # can't run headless; skipped in entry-point check
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
        fence_langs=["typescript", "tsx", "javascript", "json"],
        coding_standards=quality.BROWSER_EXT_CODING_STANDARDS,
        project_structure=quality.BROWSER_EXT_PROJECT_STRUCTURE,
        anti_patterns=quality.BROWSER_EXT_ANTI_PATTERNS,
        few_shot_scaffold=quality.BROWSER_EXT_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.BROWSER_EXT_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.BROWSER_EXT_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.BROWSER_EXT_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.BROWSER_EXT_FEW_SHOT_NAMING,
        iterate_prompt=quality.BROWSER_EXT_ITERATE_PROMPT,
        iterate_feature_prompt=quality.BROWSER_EXT_ITERATE_FEATURE_PROMPT,
    )


def go_language() -> Language:
    """Create Language config for Go projects (modules-based, gofmt'd)."""
    return Language(
        name="go",
        family="compiled",
        extensions=[".go"],
        entry_point="main.go",
        init_file=None,
        test_prefix="",
        test_suffix="_test",
        package_file="go.mod",
        install_cmd="go mod tidy",
        run_cmd="go run",
        build_cmd="go build ./...",
        test_cmd=["go", "test", "./..."],
        lint_cmd=["go", "vet", "./..."],
        syntax_check_cmd=["go", "build", "./..."],
        stdlib_modules=_GO_STDLIB,
        # Go imports: `import "path"` or `import ( "a"; "b" )`. We capture
        # the path inside quotes; multi-line blocks are caught by repeated matches.
        import_pattern=re.compile(r'^\s*(?:import\s+)?["]([^"]+)["]'),
        fence_langs=["go"],
        coding_standards=quality.GO_CODING_STANDARDS,
        project_structure=quality.GO_PROJECT_STRUCTURE,
        anti_patterns=quality.GO_ANTI_PATTERNS,
        few_shot_scaffold=quality.GO_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.GO_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.GO_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.GO_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.GO_FEW_SHOT_NAMING,
        iterate_prompt=quality.GO_ITERATE_PROMPT,
        iterate_feature_prompt=quality.GO_ITERATE_FEATURE_PROMPT,
    )


def rust_language() -> Language:
    """Create Language config for Rust projects (Cargo, 2021 edition)."""
    return Language(
        name="rust",
        family="compiled",
        extensions=[".rs"],
        entry_point="src/main.rs",
        init_file=None,
        test_prefix="",
        test_suffix="",
        package_file="Cargo.toml",
        install_cmd="cargo fetch",
        run_cmd="cargo run --",
        build_cmd="cargo build",
        test_cmd=["cargo", "test"],
        lint_cmd=["cargo", "clippy", "--", "-D", "warnings"],
        syntax_check_cmd=["cargo", "check"],
        stdlib_modules=_RUST_STDLIB,
        # Rust: `use foo::bar::Baz;` or `use foo::*;`. Capture the root crate.
        import_pattern=re.compile(r'^\s*use\s+([A-Za-z_][A-Za-z0-9_:]*)'),
        fence_langs=["rust"],
        coding_standards=quality.RUST_CODING_STANDARDS,
        project_structure=quality.RUST_PROJECT_STRUCTURE,
        anti_patterns=quality.RUST_ANTI_PATTERNS,
        few_shot_scaffold=quality.RUST_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.RUST_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.RUST_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.RUST_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.RUST_FEW_SHOT_NAMING,
        iterate_prompt=quality.RUST_ITERATE_PROMPT,
        iterate_feature_prompt=quality.RUST_ITERATE_FEATURE_PROMPT,
    )


def electron_language() -> Language:
    """Create Language config for Electron + React + TypeScript desktop apps.

    Same node toolchain as react_language() — npm, vite, tsc, eslint, vitest —
    plus electron + electron-builder + vite-plugin-electron. The OS launches
    the main process (`src/main/main.ts`) which owns the BrowserWindow; the
    renderer is a regular React app loaded inside it.

    `build_cmd` here is the renderer build only (`npx vite build`). The
    full installer step (`electron-builder --win`) is intentionally NOT in
    `build_cmd` because (a) it downloads ~100MB of toolchain on first run,
    (b) the validation pipeline runs `build_cmd` and shouldn't pay that
    cost on every check, and (c) `.split()` on a `&&`-chained command would
    pass `&&` as an argv token. Users / CI run electron-builder directly.
    """
    return Language(
        name="electron",
        family="node",
        extensions=[".tsx", ".ts", ".jsx", ".js", ".css", ".html"],
        entry_point="src/main/main.ts",
        init_file="index.ts",
        test_prefix="",
        test_suffix=".test",
        package_file="package.json",
        install_cmd="npm install",
        run_cmd="npx electron .",
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
        coding_standards=quality.ELECTRON_CODING_STANDARDS,
        project_structure=quality.ELECTRON_PROJECT_STRUCTURE,
        anti_patterns=quality.ELECTRON_ANTI_PATTERNS,
        few_shot_scaffold=quality.ELECTRON_FEW_SHOT_SCAFFOLD,
        few_shot_main=quality.ELECTRON_FEW_SHOT_MAIN,
        few_shot_db_pattern=quality.ELECTRON_FEW_SHOT_DB_PATTERN,
        functional_test_guidance=quality.ELECTRON_FUNCTIONAL_TEST_GUIDANCE,
        few_shot_naming=quality.ELECTRON_FEW_SHOT_NAMING,
        iterate_prompt=quality.ELECTRON_ITERATE_PROMPT,
        iterate_feature_prompt=quality.ELECTRON_ITERATE_FEATURE_PROMPT,
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
    "desktop app", "windows app", "windows native", "windows desktop",
    "native desktop", "cross-platform desktop",
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

_GO_KEYWORDS = frozenset({
    "golang", "go module", "go service", "go cli", "go binary",
    "go.mod", "gofmt", "goroutine", "go test",
})

_WORDPRESS_KEYWORDS = frozenset({
    "wordpress", "wp plugin", "wordpress plugin", "wp-plugin",
    "wp.org", "wp_", "wp shortcode", "wp admin", "gutenberg block",
})

_BROWSER_EXT_KEYWORDS = frozenset({
    "browser extension", "chrome extension", "firefox extension",
    "web extension", "manifest v3", "mv3", "chrome plugin",
    "browser plugin", "extension popup",
})

# PyTorch keywords. Distinct from python_language() because the LLM gets
# wildly different guidance on a "Flask app" vs. a "PyTorch training loop".
# "torch" alone is too generic (could be unrelated); require an ML signal
# alongside it (training, model, fine-tune, neural, etc.) — see detect_language.
_PYTORCH_HARD_KEYWORDS = frozenset({
    "pytorch", "torch.nn", "torch.cuda", "torch.optim", "fine-tune",
    "fine tune", "training loop", "model checkpoint", "deep learning",
    "neural network", "neural net", "transformer model", "lora",
})
_PYTORCH_SOFT_KEYWORDS = frozenset({
    "torch", "cuda", "gpu", "model", "training", "inference",
    "backprop", "gradient", "loss", "tensor",
})

_RUST_KEYWORDS = frozenset({
    "rust", "cargo", "rustc", "rust crate", "cargo.toml", "rust binary",
    "rust library", "tokio", "serde",
})


def _augment_for_web_game(lang: Language, task: str) -> Language:
    """Append the web-game / PWA addendum to `lang.anti_patterns` when the
    task keywords suggest a canvas game or PWA. No-op otherwise. Returns the
    same lang (mutated in place — Language is not frozen)."""
    addendum = quality.web_game_addendum(task)
    if addendum:
        lang.anti_patterns = (lang.anti_patterns or "") + addendum
    return lang


def detect_language(task: str, workspace: str | None = None) -> Language:
    """Detect target language from task description or existing workspace."""
    task_lower = task.lower()

    # Plain HTML/CSS/JS — check BEFORE TS keywords (which include "html", "css", etc.)
    # But Python keywords override HTML (e.g., "Flask + vanilla HTML" = Python)
    if any(kw in task_lower for kw in _HTML_INDICATORS):
        if not any(kw in task_lower for kw in _FRAMEWORK_KEYWORDS):
            if not any(kw in task_lower for kw in _PYTHON_KEYWORDS):
                return _augment_for_web_game(html_language(), task)

    # Platform-specific detection — check BEFORE generic frameworks.
    # Browser extension / WordPress are NOT just React or PHP apps; they
    # have specific manifests/structure. Word-boundary regex per keyword
    # so "extension" embedded in "extensible" doesn't false-trigger.
    if any(re.search(rf"\b{re.escape(kw)}\b", task_lower) for kw in _WORDPRESS_KEYWORDS):
        return wordpress_language()
    if any(re.search(rf"\b{re.escape(kw)}\b", task_lower) for kw in _BROWSER_EXT_KEYWORDS):
        return browser_extension_language()

    # PyTorch / GPU ML — a Python-family overlay with richer guidance.
    # Either an explicit hard keyword, OR "torch" / "cuda" + an ML soft
    # keyword. This avoids routing "a torch app for a flashlight UI" to
    # PyTorch but catches "fine-tune a transformer on cuda" cleanly.
    if any(re.search(rf"\b{re.escape(kw)}\b", task_lower) for kw in _PYTORCH_HARD_KEYWORDS):
        return pytorch_language()
    if (
        re.search(r"\btorch\b", task_lower)
        or re.search(r"\bcuda\b", task_lower)
    ) and any(
        re.search(rf"\b{re.escape(kw)}\b", task_lower)
        for kw in _PYTORCH_SOFT_KEYWORDS - {"torch", "cuda"}
    ):
        return pytorch_language()

    # Compiled-language detection — check BEFORE Python/JS so a task that
    # mentions "rust web service" or "go cli with serde" routes correctly.
    # Use word-boundary checks for "rust" / "go" to avoid false positives
    # ("trustpilot" → rust, "go to" → go).
    if any(re.search(rf"\b{re.escape(kw)}\b", task_lower) for kw in _RUST_KEYWORDS):
        return rust_language()
    if any(re.search(rf"\b{re.escape(kw)}\b", task_lower) for kw in _GO_KEYWORDS):
        return go_language()

    # Framework-specific detection — check BEFORE generic JS/TS.
    # Electron must come BEFORE react: a task like "electron + react desktop
    # app" mentions both keywords but should route to electron (the renderer
    # is React, the wrapper is electron — different toolchain, different
    # entry point, different security model).
    if any(kw in task_lower for kw in (
        "electron", "windows app", "windows native", "windows desktop",
        "desktop app", "cross-platform desktop", "native desktop",
    )):
        return electron_language()
    if any(kw in task_lower for kw in ("react", "nextjs", "next.js")):
        return _augment_for_web_game(react_language(), task)
    if "vue" in task_lower:
        return _augment_for_web_game(vue_language(), task)
    if "angular" in task_lower:
        return _augment_for_web_game(angular_language(), task)

    # Python-specific keywords — check BEFORE JS keywords to prevent
    # false positives (e.g., "Flask REST API" should be Python, not TS)
    if any(kw in task_lower for kw in _PYTHON_KEYWORDS):
        return python_language()

    if any(kw in task_lower for kw in _JS_KEYWORDS):
        return typescript_language()
    # Workspace fallback. Prefer what the code ACTUALLY IS over the presence of
    # a single marker file: `iterate()` calls this with an empty task string, so
    # this branch decides the language for the whole post-build phase.
    #
    # Trusting package.json alone misrouted a 21-file Python FastAPI project to
    # typescript, because a spurious "unresolved import 'aiosqlite'" had led the
    # model to call add_dep(), which wrote a package.json containing a PYTHON
    # package. Every subsequent validation then ran as TS: "node_modules
    # missing", ".test.ts" discovery, "no tsconfig.json". Counting source files
    # makes that impossible — one stray manifest cannot outvote the tree.
    if workspace:
        dominant = _dominant_source_language(workspace)
        if dominant is not None:
            return dominant
        if os.path.exists(os.path.join(workspace, "package.json")):
            return typescript_language()
    return python_language()


def _dominant_source_language(workspace: str):
    """Language implied by the source files present, or None when ambiguous.

    Counts hand-written sources only — generated/vendored trees would otherwise
    swamp the signal. Returns None when there is nothing to go on, so callers
    keep their existing fallbacks.
    """
    skip = {"__pycache__", "node_modules", ".git", ".cadillac", ".venv",
            "venv", "dist", "build", "site-packages"}
    counts: dict[str, int] = {}
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            for ext, key in ((".py", "python"), (".ts", "ts"), (".tsx", "ts"),
                             (".js", "ts"), (".jsx", "ts"), (".go", "go"),
                             (".rs", "rust")):
                if f.endswith(ext):
                    counts[key] = counts.get(key, 0) + 1
                    break
    if not counts:
        return None
    best, n = max(counts.items(), key=lambda kv: kv[1])
    # Require a clear majority; a mixed tree stays ambiguous for the caller.
    if n < 2 or n <= sum(counts.values()) / 2:
        return None
    return {
        "python": python_language,
        "ts": typescript_language,
        "go": go_language,
        "rust": rust_language,
    }[best]()
