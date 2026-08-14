"""Tests for the redaction layer.

Cadillac had no redaction. Three sinks made that a real exposure: build.jsonl
(every event, verbatim, inside the workspace), memory.jsonl (lessons that
outlive the build and are re-injected into every future prompt), and
progress.md (also inside the workspace). The workspace is the published
deliverable.

The asymmetry that shapes every rule here: a false negative leaks a
credential, a false positive corrupts generated source code. So the patterns
match shapes that are ONLY ever secrets, and the "must not touch" side of each
rule is pinned as hard as the "must redact" side.
"""

import json
import os
import tempfile
import unittest

from cadillac.redact import (
    REDACTED,
    redact,
    redact_home_path,
    redact_obj,
    redact_secrets,
)


class TestProviderKeys(unittest.TestCase):
    def test_openai_key(self):
        out = redact_secrets("export OPENAI_API_KEY=sk-abcdefghij0123456789ABCDEF")
        self.assertNotIn("sk-abcdefghij0123456789", out)
        self.assertIn(REDACTED, out)

    def test_anthropic_key(self):
        self.assertIn(REDACTED, redact_secrets("key: sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"))

    def test_google_key(self):
        self.assertIn(REDACTED, redact_secrets("AIzaSyA1234567890abcdefghijklmnopqrs"))

    def test_github_token(self):
        self.assertIn(REDACTED, redact_secrets("ghp_0123456789abcdefghijklmnopqrstuvwxyz"))

    def test_slack_token(self):
        self.assertIn(REDACTED, redact_secrets("xoxb-123456789012-abcdefghijkl"))

    def test_aws_access_key_id(self):
        self.assertIn(REDACTED, redact_secrets("AKIAIOSFODNN7EXAMPLE"))

    def test_huggingface_token(self):
        self.assertIn(REDACTED, redact_secrets("hf_ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"))

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
        self.assertIn(REDACTED, redact_secrets(jwt))

    def test_pem_block_collapsed(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAvxxxxx\nyyyyyy\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_secrets(pem)
        self.assertNotIn("MIIEowIBAAKCAQEA", out)
        self.assertIn("PRIVATE-KEY", out)


class TestHeaders(unittest.TestCase):
    def test_bearer_token(self):
        out = redact_secrets("Authorization: Bearer abc123def456ghi789")
        self.assertNotIn("abc123def456ghi789", out)
        self.assertIn("Bearer", out, "shape should survive so the log stays readable")

    def test_x_api_key_header(self):
        out = redact_secrets("X-API-Key: 9f8e7d6c5b4a39281706")
        self.assertNotIn("9f8e7d6c5b4a39281706", out)

    def test_curl_header_flag(self):
        out = redact_secrets("curl -H 'Authorization: Bearer sk-abcdefghijklmnop1234' http://x")
        self.assertNotIn("sk-abcdefghijklmnop1234", out)


class TestAssignments(unittest.TestCase):
    def test_api_key_assignment(self):
        out = redact_secrets("OPENAI_API_KEY=hunter2hunter2hunter2")
        self.assertNotIn("hunter2hunter2hunter2", out)

    def test_json_secret_field(self):
        out = redact_secrets('{"jwt_secret": "s3cr3tv4lu3long"}')
        self.assertNotIn("s3cr3tv4lu3long", out)

    def test_python_password_assignment(self):
        out = redact_secrets('DB_PASSWORD = "correcthorsebattery"')
        self.assertNotIn("correcthorsebattery", out)

    def test_access_key_assignment(self):
        out = redact_secrets("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCY")
        self.assertNotIn("wJalrXUtnFEMIK7MDENGbPxRfiCY", out)

    # -- the side that protects generated code from corruption --
    def test_env_var_indirection_preserved(self):
        """`API_KEY=$OPENAI_API_KEY` is a reference, not a value."""
        text = "API_KEY=$OPENAI_API_KEY"
        self.assertEqual(redact_secrets(text), text)

    def test_process_env_preserved(self):
        text = 'const apiKey = process.env.OPENAI_API_KEY;'
        self.assertEqual(redact_secrets(text), text)

    def test_os_environ_preserved(self):
        text = 'api_key = os.environ["OPENAI_API_KEY"]'
        self.assertEqual(redact_secrets(text), text)

    def test_placeholder_preserved(self):
        text = "API_KEY=your-api-key-here"
        self.assertEqual(redact_secrets(text), text)

    def test_short_value_preserved(self):
        text = "TOKEN=abc"
        self.assertEqual(redact_secrets(text), text)

    def test_ordinary_code_untouched(self):
        for text in [
            "def get_token(request):\n    return request.headers.get('X-Token')",
            "import os\nfrom flask import Flask\napp = Flask(__name__)",
            "const [count, setCount] = useState(0);",
            "SELECT id, name FROM users WHERE active = 1",
            "# TODO: add authentication middleware",
            "expect(response.status).toBe(200);",
        ]:
            self.assertEqual(redact_secrets(text), text, f"mangled: {text!r}")


class TestHomePath(unittest.TestCase):
    def test_home_collapsed(self):
        home = os.path.expanduser("~")
        self.assertEqual(redact_home_path(f"{home}/sandbox/x"), "~/sandbox/x")

    def test_relative_path_untouched(self):
        self.assertEqual(redact_home_path("src/app.py"), "src/app.py")

    def test_full_redact_does_both(self):
        home = os.path.expanduser("~")
        out = redact(f"{home}/x OPENAI_API_KEY=sk-abcdefghij0123456789ABCDEF")
        self.assertIn("~/x", out)
        self.assertIn(REDACTED, out)


class TestRedactObj(unittest.TestCase):
    def test_nested_dict(self):
        obj = {"kind": "tool", "data": {"stderr": "OPENAI_API_KEY=sk-abcdefghij0123456789ABC"}}
        out = redact_obj(obj)
        self.assertNotIn("sk-abcdefghij0123456789ABC", json.dumps(out))

    def test_list_of_strings(self):
        out = redact_obj(["ghp_0123456789abcdefghijklmnopqrstuvwxyz", "ok"])
        self.assertIn(REDACTED, out[0])
        self.assertEqual(out[1], "ok")

    def test_non_strings_preserved(self):
        obj = {"n": 42, "f": 1.5, "b": True, "z": None}
        self.assertEqual(redact_obj(obj), obj)

    def test_deep_nesting_terminates(self):
        obj = cur = {}
        for _ in range(50):
            cur["next"] = {}
            cur = cur["next"]
        redact_obj(obj)  # must not raise

    def test_tuple_type_preserved(self):
        self.assertIsInstance(redact_obj(("a", "b")), tuple)


class TestBuildLoggerRedaction(unittest.TestCase):
    """The primary sink: build.jsonl lives inside the published workspace."""

    def test_event_is_redacted_on_disk(self):
        from cadillac.engine import BuildLogger
        from cadillac.events import Event

        with tempfile.TemporaryDirectory() as td:
            logger = BuildLogger(td)
            event = Event(kind="tool", data={
                "stderr": "error: OPENAI_API_KEY=sk-abcdefghij0123456789ABCDEF is invalid"
            })
            logger.handler(event)
            logger.close()
            with open(os.path.join(td, ".cadillac", "build.jsonl")) as f:
                content = f.read()
        self.assertNotIn("sk-abcdefghij0123456789ABCDEF", content)
        self.assertIn(REDACTED, content)

    def test_ordinary_event_survives_intact(self):
        from cadillac.engine import BuildLogger
        from cadillac.events import Event

        with tempfile.TemporaryDirectory() as td:
            logger = BuildLogger(td)
            logger.handler(Event(kind="log", data={"msg": "[BUILD] wrote src/app.py"}))
            logger.close()
            with open(os.path.join(td, ".cadillac", "build.jsonl")) as f:
                record = json.loads(f.read().strip())
        self.assertEqual(record["msg"], "[BUILD] wrote src/app.py")


class TestMemoryRedaction(unittest.TestCase):
    """Lessons outlive the build and re-enter every future prompt."""

    def test_lesson_is_redacted_before_persistence(self):
        import cadillac.memory as memory
        from cadillac.memory import Lesson, save_lesson

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "memory.jsonl")
            original = memory.MEMORY_PATH
            memory.MEMORY_PATH = path
            try:
                save_lesson(Lesson(
                    ts=0.0,
                    type="error",
                    trigger="auth fails with OPENAI_API_KEY=sk-abcdefghij0123456789ABCDEF",
                    fix="use the env var",
                ))
                with open(path) as f:
                    content = f.read()
            finally:
                memory.MEMORY_PATH = original
        self.assertNotIn("sk-abcdefghij0123456789ABCDEF", content)


class TestGitignoreScaffold(unittest.TestCase):
    """Without a .gitignore, `git add .` in a generated project commits the
    entire build trace."""

    def test_creates_gitignore_with_cadillac_dir(self):
        from cadillac.engine import _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            _write_gitignore(td)
            with open(os.path.join(td, ".gitignore")) as f:
                content = f.read()
        self.assertIn(".cadillac/", content)
        self.assertIn(".env", content)
        self.assertIn("progress.md", content)

    def test_merges_into_existing_without_clobbering(self):
        from cadillac.engine import _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, ".gitignore")
            with open(path, "w") as f:
                f.write("# project rules\ncoverage/\n")
            _write_gitignore(td)
            with open(path) as f:
                content = f.read()
        self.assertIn("coverage/", content, "existing entries must survive")
        self.assertIn(".cadillac/", content)

    def test_idempotent(self):
        from cadillac.engine import _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            _write_gitignore(td)
            with open(os.path.join(td, ".gitignore")) as f:
                first = f.read()
            _write_gitignore(td)
            with open(os.path.join(td, ".gitignore")) as f:
                second = f.read()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()


class TestGitignoreBeatsGitInit(unittest.TestCase):
    """The .gitignore must exist BEFORE Cadillac's first `git add -A`.

    `_git_init` runs early (rollback support) and stages everything. Writing
    the ignore file only at PACKAGE was too late: `.cadillac/` was already
    tracked by commit 1, and .gitignore has no effect on a tracked path, so a
    published workspace still carried its whole build trace. Caught by
    inspecting a real workspace from a CLI run — `git add .` still staged
    `.cadillac/build.jsonl` despite a correct-looking .gitignore.
    """

    def _workspace(self, td):
        os.makedirs(os.path.join(td, ".cadillac"))
        os.makedirs(os.path.join(td, "src"))
        with open(os.path.join(td, ".cadillac", "build.jsonl"), "w") as f:
            f.write('{"ts":1,"kind":"log","msg":"hi"}\n')
        with open(os.path.join(td, ".cadillac", "checkpoint.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(td, "src", "app.py"), "w") as f:
            f.write("print(1)\n")
        with open(os.path.join(td, ".env"), "w") as f:
            f.write("SECRET=x\n")

    def _tracked(self, td):
        import subprocess
        out = subprocess.run(["git", "ls-files"], cwd=td,
                             capture_output=True, text=True)
        return set(out.stdout.split())

    def test_git_init_does_not_track_build_artifacts(self):
        from cadillac.engine import _git_init

        with tempfile.TemporaryDirectory() as td:
            self._workspace(td)
            _git_init(td)
            tracked = self._tracked(td)
        self.assertNotIn(".cadillac/build.jsonl", tracked)
        self.assertNotIn(".cadillac/checkpoint.json", tracked)
        self.assertNotIn(".env", tracked)

    def test_git_init_still_tracks_real_source(self):
        from cadillac.engine import _git_init

        with tempfile.TemporaryDirectory() as td:
            self._workspace(td)
            _git_init(td)
            tracked = self._tracked(td)
        self.assertIn("src/app.py", tracked)
        self.assertIn(".gitignore", tracked)

    def test_untrack_repairs_a_polluted_index(self):
        """Workspaces whose repo predates the fix must still be repairable."""
        import subprocess

        from cadillac.engine import _git_untrack_build_artifacts, _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            self._workspace(td)
            # Simulate the OLD behaviour: init and stage with no .gitignore.
            for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                         "commit", "-q", "-m", "initial"]):
                subprocess.run(cmd, cwd=td, capture_output=True)
            self.assertIn(".cadillac/build.jsonl", self._tracked(td))

            # build.jsonl keeps growing during a build, which is what made a
            # plain `git rm --cached` refuse.
            with open(os.path.join(td, ".cadillac", "build.jsonl"), "a") as f:
                f.write('{"ts":2,"kind":"log","msg":"more"}\n')
            subprocess.run(["git", "add", "-A"], cwd=td, capture_output=True)
            with open(os.path.join(td, ".cadillac", "build.jsonl"), "a") as f:
                f.write('{"ts":3,"kind":"log","msg":"even more"}\n')

            _write_gitignore(td)
            self.assertTrue(_git_untrack_build_artifacts(td))
            tracked = self._tracked(td)
            self.assertNotIn(".cadillac/build.jsonl", tracked)
            self.assertIn("src/app.py", tracked)
            # index-only: the file must survive on disk
            self.assertTrue(os.path.exists(os.path.join(td, ".cadillac", "build.jsonl")))

    def test_untrack_is_idempotent_and_honest(self):
        from cadillac.engine import _git_init, _git_untrack_build_artifacts

        with tempfile.TemporaryDirectory() as td:
            self._workspace(td)
            _git_init(td)
            # Nothing to untrack after the fix, and it must SAY so rather than
            # reporting success unconditionally (the first version did).
            self.assertFalse(_git_untrack_build_artifacts(td))

    def test_untrack_on_non_git_dir_is_false(self):
        from cadillac.engine import _git_untrack_build_artifacts

        with tempfile.TemporaryDirectory() as td:
            self._workspace(td)
            self.assertFalse(_git_untrack_build_artifacts(td))


class TestUntrackCoversEveryIgnoredPath(unittest.TestCase):
    """The repair must untrack everything the .gitignore excludes, not just
    `.cadillac`.

    A completed real build was repaired with a `.cadillac`-only version and was
    still tracking 20+ `__pycache__/*.pyc` files afterwards. Asking git which
    tracked files are ignored (`ls-files -i -c --exclude-standard`) covers
    .pyc, node_modules, dist and .env for free.
    """

    def _polluted_repo(self, td):
        import subprocess
        for rel in ("src/app.py", "src/__pycache__/app.cpython-312.pyc",
                    "__pycache__/main.cpython-312.pyc",
                    ".cadillac/build.jsonl", "node_modules/dep/index.js",
                    "dist/bundle.js", ".env"):
            path = os.path.join(td, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x\n")
        # OLD behaviour: init and stage with no .gitignore at all.
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-q", "-m", "initial"]):
            subprocess.run(cmd, cwd=td, capture_output=True)

    def _tracked(self, td):
        import subprocess
        return set(subprocess.run(["git", "ls-files"], cwd=td,
                                  capture_output=True, text=True).stdout.split())

    def test_untracks_pyc_and_vendor_dirs_too(self):
        from cadillac.engine import _git_untrack_build_artifacts, _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            self._polluted_repo(td)
            self.assertIn("src/__pycache__/app.cpython-312.pyc", self._tracked(td))

            _write_gitignore(td)
            self.assertTrue(_git_untrack_build_artifacts(td))
            tracked = self._tracked(td)

        self.assertEqual([p for p in tracked if p.endswith(".pyc")], [])
        for prefix in (".cadillac/", "node_modules/", "dist/"):
            self.assertEqual([p for p in tracked if p.startswith(prefix)], [], prefix)
        self.assertNotIn(".env", tracked)

    def test_real_source_survives_the_repair(self):
        from cadillac.engine import _git_untrack_build_artifacts, _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            self._polluted_repo(td)
            _write_gitignore(td)
            _git_untrack_build_artifacts(td)
            tracked = self._tracked(td)
            gitignore_on_disk = os.path.exists(os.path.join(td, ".gitignore"))
        self.assertIn("src/app.py", tracked, "the repair must not touch real source")
        # .gitignore is not in the index here because this fixture commits
        # BEFORE writing it (reproducing the old broken order). It only needs
        # to exist on disk; the ignore-at-init path stages it, and that is
        # covered by TestGitignoreBeatsGitInit.
        self.assertTrue(gitignore_on_disk)

    def test_ignored_files_remain_on_disk(self):
        """Index-only: the repair must never delete anything."""
        from cadillac.engine import _git_untrack_build_artifacts, _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            self._polluted_repo(td)
            _write_gitignore(td)
            _git_untrack_build_artifacts(td)
            for rel in (".cadillac/build.jsonl", "dist/bundle.js", ".env",
                        "src/__pycache__/app.cpython-312.pyc"):
                self.assertTrue(os.path.exists(os.path.join(td, rel)), rel)

    def test_second_repair_reports_nothing_to_do(self):
        from cadillac.engine import _git_untrack_build_artifacts, _write_gitignore

        with tempfile.TemporaryDirectory() as td:
            self._polluted_repo(td)
            _write_gitignore(td)
            self.assertTrue(_git_untrack_build_artifacts(td))
            self.assertFalse(_git_untrack_build_artifacts(td))
