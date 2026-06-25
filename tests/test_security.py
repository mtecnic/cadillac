"""Tests for the security check (Phase 1.2 of weakness roadmap).

Each pattern in `_SEC_PATTERNS` is exercised with a positive case (must
fire) and a negative case (must NOT fire). False positives erode trust
faster than false negatives — every test pins both sides.
"""

import json
import os
import tempfile
import unittest

from cadillac.languages import python_language, react_language
from cadillac.validate import check_security


def _write(td: str, rel: str, content: str) -> None:
    path = os.path.join(td, rel)
    os.makedirs(os.path.dirname(path) or td, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _findings(td: str, lang=None) -> tuple[list, list]:
    """Run check_security; return (errors, warnings) result lists."""
    results = check_security(td, lang or python_language())
    errors = [r for r in results if not r.passed and r.severity == "error"]
    warnings = [r for r in results if not r.passed and r.severity == "warning"]
    return errors, warnings


class TestEmptyWorkspace(unittest.TestCase):
    def test_no_findings_passes(self):
        with tempfile.TemporaryDirectory() as td:
            results = check_security(td, python_language())
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].passed)
            self.assertIn("no obvious security issues", results[0].output)


class TestHardcodedSecrets(unittest.TestCase):
    def test_password_literal_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "auth.py", 'password = "supersecret123"\n')
            errors, _ = _findings(td)
            self.assertTrue(any("hardcoded_secret" in e.output for e in errors),
                            f"expected hardcoded_secret HIGH, got: {[e.output for e in errors]}")

    def test_jwt_secret_literal_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "auth.py", 'JWT_SECRET = "dev-secret-please-change"\n')
            errors, _ = _findings(td)
            self.assertTrue(any("hardcoded_secret" in e.output for e in errors))

    def test_password_from_env_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "auth.py",
                   'import os\npassword = os.environ.get("PASSWORD")\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])

    def test_short_placeholder_not_flagged(self):
        # `password = ""` is an empty default, not a leak.
        with tempfile.TemporaryDirectory() as td:
            _write(td, "config.py", 'password = ""\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])

    def test_aws_access_key(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "deploy.py", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
            errors, _ = _findings(td)
            self.assertTrue(any("aws_access_key" in e.output for e in errors))


class TestSqlInjection(unittest.TestCase):
    def test_fstring_in_execute_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "db.py",
                   'def get(id):\n'
                   '    return cursor.execute(f"SELECT * FROM users WHERE id={id}")\n')
            errors, _ = _findings(td)
            self.assertTrue(any("sql_fstring" in e.output for e in errors))

    def test_concat_in_execute_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "db.py",
                   'def get(name):\n'
                   '    return cursor.execute("SELECT * FROM x WHERE name=" + name)\n')
            errors, _ = _findings(td)
            self.assertTrue(any("sql_concat" in e.output for e in errors))

    def test_parameterized_query_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "db.py",
                   'def get(id):\n'
                   '    return cursor.execute("SELECT * FROM users WHERE id=?", (id,))\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])

    def test_schema_identifier_constants_not_flagged(self):
        """Real false positive from the 2026-06-25 habit-tracker build:
        f"INSERT INTO {USERS_TABLE} ({U_EMAIL}) VALUES (?)" was flagged
        18× even though the only interpolations are module-level
        identifier constants and the value position uses `?` parameters.

        Constants are uppercase or have a known suffix (_TABLE, _COL, etc).
        The execute call spans multiple lines so the FP guard has to
        look at the lines covered by the regex match, not just the
        line where the match started.
        """
        with tempfile.TemporaryDirectory() as td:
            _write(td, "repo.py",
                   'USERS_TABLE = "users"\n'
                   'U_EMAIL = "email"\n'
                   'U_PW = "password_hash"\n'
                   'async def create(conn, email, pw):\n'
                   '    cursor = await conn.execute(\n'
                   '        f"INSERT INTO {USERS_TABLE} ({U_EMAIL}, {U_PW}) VALUES (?, ?)",\n'
                   '        (email, pw),\n'
                   '    )\n')
            errors, _ = _findings(td)
            self.assertFalse(
                any("sql_fstring" in e.output for e in errors),
                f"schema-identifier-constant pattern must not be flagged; got: {errors}",
            )

    def test_schema_identifier_pattern_requires_question_mark(self):
        """The schema-constant guard ONLY fires when `?` parameters are
        present in the same statement. A naked f-string with NO `?` and
        only constants is still suspicious (the values must be coming
        from somewhere) — keep flagging it."""
        with tempfile.TemporaryDirectory() as td:
            # No `?` placeholder — the constants are the only interpolation
            # but there's no parameterized value position. Stays flagged.
            _write(td, "repo.py",
                   'TABLE = "users"\n'
                   'def get():\n'
                   '    return conn.execute(f"SELECT * FROM {TABLE}")\n')
            errors, _ = _findings(td)
            self.assertTrue(
                any("sql_fstring" in e.output for e in errors),
                "no `?` means we can't prove values are parameterized; must flag",
            )


class TestCommandInjection(unittest.TestCase):
    def test_shell_true_with_fstring(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "ops.py",
                   'import subprocess\n'
                   'def run(cmd):\n'
                   '    return subprocess.run(f"sh -c \'{cmd}\'", shell=True)\n')
            errors, _ = _findings(td)
            self.assertTrue(any("shell_true_with_interp" in e.output for e in errors))

    def test_shell_true_literal_arg_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "ops.py",
                   'import subprocess\n'
                   'subprocess.run("ls -la", shell=True)\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])

    def test_argv_list_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "ops.py",
                   'import subprocess\n'
                   'subprocess.run(["ls", "-la"])\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])


class TestEvalExec(unittest.TestCase):
    def test_eval_on_input_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "calc.py",
                   'def evaluate(expr):\n'
                   '    return eval(input("expr: "))\n')
            errors, _ = _findings(td)
            self.assertTrue(any("eval_user_input" in e.output for e in errors))

    def test_eval_on_literal_not_flagged(self):
        # eval() of a literal is dumb but not RCE — don't false-positive.
        with tempfile.TemporaryDirectory() as td:
            _write(td, "calc.py", 'x = eval("1 + 1")\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])


class TestJwt(unittest.TestCase):
    def test_alg_none_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "auth.py",
                   'import jwt\n'
                   'token = jwt.encode(payload, key, algorithm="none")\n')
            errors, _ = _findings(td)
            self.assertTrue(any("jwt_alg_none" in e.output for e in errors))


class TestWeakCrypto(unittest.TestCase):
    def test_md5_for_password_warning(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "auth.py",
                   'import hashlib\n'
                   'def hash_password(password):\n'
                   '    return hashlib.md5(password.encode()).hexdigest()\n')
            _, warnings = _findings(td)
            self.assertTrue(any("weak_password_hash" in w.output for w in warnings))

    def test_md5_on_id_not_flagged(self):
        # md5() of a non-credential value is fine (e.g., cache keys, ETags).
        with tempfile.TemporaryDirectory() as td:
            _write(td, "cache.py",
                   'import hashlib\n'
                   'def cache_key(url):\n'
                   '    return hashlib.md5(url.encode()).hexdigest()\n')
            _, warnings = _findings(td)
            self.assertFalse(any("weak_password_hash" in w.output for w in warnings))


class TestTlsVerify(unittest.TestCase):
    def test_verify_false_warning(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "client.py",
                   'import requests\nr = requests.get("https://x.test", verify=False)\n')
            _, warnings = _findings(td)
            self.assertTrue(any("tls_verify_disabled" in w.output for w in warnings))


class TestReactDangerouslySet(unittest.TestCase):
    def test_dangerously_set_inner_html_warning(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "Article.tsx",
                   'export function Article({ html }: { html: string }) {\n'
                   '  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n'
                   '}\n')
            _, warnings = _findings(td, react_language())
            self.assertTrue(
                any("dangerously_set_inner_html" in w.output for w in warnings),
                f"expected dangerouslySetInnerHTML warning, got warnings={[w.output for w in warnings]}",
            )


class TestSkipping(unittest.TestCase):
    def test_node_modules_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            # A real-looking secret in a vendored dep — must NOT fire.
            _write(td, "node_modules/some-pkg/index.js",
                   'const password = "leaked-from-vendor-12345";\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])

    def test_test_files_ignored(self):
        # password literal in a test fixture — fixtures are fine.
        with tempfile.TemporaryDirectory() as td:
            _write(td, "tests/test_auth.py", 'password = "fixture-password-123"\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])

    def test_pycache_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "__pycache__/x.py", 'JWT_SECRET = "leaked"\n')
            errors, _ = _findings(td)
            self.assertEqual(errors, [])


class TestPhpPatterns(unittest.TestCase):
    """PHP / WordPress-specific security patterns (Phase 2 hardening)."""

    def test_wpdb_concat_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "global $wpdb;\n"
                "$id = $_GET['id'];\n"
                "$rows = $wpdb->get_results(\"SELECT * FROM x WHERE id=\" . $id);\n"
            ))
            errors, _ = _findings(td)
            self.assertTrue(any("php_sql_concat" in e.output for e in errors))

    def test_wpdb_interpolation_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "global $wpdb;\n"
                "$id = 1;\n"
                "$rows = $wpdb->query(\"SELECT * FROM x WHERE id = $id\");\n"
            ))
            errors, _ = _findings(td)
            self.assertTrue(any("php_sql_interp" in e.output for e in errors))

    def test_wpdb_table_name_interpolation_not_flagged(self):
        """Table-name interpolation is the canonical safe pattern — $wpdb->prepare
        can't bind identifiers, so you HAVE to interpolate the table name. Don't
        false-positive on this (regression: caught in Phase 2 limit-tests where
        a real WP plugin's `DROP TABLE {$table_name}` was wrongly flagged HIGH)."""
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "global $wpdb;\n"
                "$table_name = $wpdb->prefix . 'mp_things';\n"
                "$wpdb->query(\"DROP TABLE IF EXISTS {$table_name}\");\n"
                "$count = $wpdb->get_var(\"SELECT COUNT(*) FROM {$table_name}\");\n"
            ))
            errors, _ = _findings(td)
            self.assertEqual(
                [e for e in errors if "php_sql_interp" in e.output], [],
                "table-name interpolation should NOT be flagged as SQL injection",
            )

    def test_wpdb_prepare_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "global $wpdb;\n"
                "$id = 1;\n"
                "$rows = $wpdb->get_results( $wpdb->prepare(\n"
                "    \"SELECT * FROM x WHERE id = %d\", $id ) );\n"
            ))
            errors, _ = _findings(td)
            sql = [e for e in errors if "php_sql" in e.output]
            self.assertEqual(sql, [])

    def test_unescaped_echo_of_user_input_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "echo $_GET['name'];\n"
            ))
            errors, _ = _findings(td)
            self.assertTrue(any("php_unescaped_echo" in e.output for e in errors))

    def test_escaped_echo_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "echo esc_html( $_GET['name'] ?? '' );\n"
            ))
            errors, _ = _findings(td)
            self.assertEqual([e for e in errors if "php_unescaped_echo" in e.output], [])

    def test_shell_exec_with_var_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "$cmd = $_POST['cmd'];\n"
                "shell_exec(\"ls $cmd\");\n"
            ))
            errors, _ = _findings(td)
            self.assertTrue(any("php_shell_exec_interp" in e.output for e in errors))

    def test_unserialize_user_input_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "$obj = unserialize($_POST['data']);\n"
            ))
            errors, _ = _findings(td)
            self.assertTrue(any("php_unserialize_user_input" in e.output for e in errors))

    def test_include_user_input_high(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "include $_GET['page'];\n"
            ))
            errors, _ = _findings(td)
            self.assertTrue(any("php_include_var" in e.output for e in errors))

    def test_missing_abspath_guard_warning(self):
        with tempfile.TemporaryDirectory() as td:
            # Plugin file with class but NO ABSPATH guard
            _write(td, "plugin.php", (
                "<?php\n"
                "class MP_Settings {\n"
                "    public static function init() { /* ... */ }\n"
                "}\n"
            ))
            _, warnings = _findings(td)
            self.assertTrue(
                any("wp_no_abspath_guard" in w.output for w in warnings),
                f"expected ABSPATH-guard warning, got: {[w.output for w in warnings]}",
            )

    def test_abspath_guard_present_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "plugin.php", (
                "<?php\n"
                "if ( ! defined( 'ABSPATH' ) ) { exit; }\n"
                "class MP_Settings {\n"
                "    public static function init() { /* ... */ }\n"
                "}\n"
            ))
            _, warnings = _findings(td)
            self.assertFalse(any("wp_no_abspath_guard" in w.output for w in warnings))

    def test_vendor_dir_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            # composer-installed dep with a "leak" — must NOT fire
            _write(td, "vendor/some-pkg/src/X.php", (
                "<?php\n"
                "$password = \"vendored-secret-12345\";\n"
            ))
            errors, _ = _findings(td)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
