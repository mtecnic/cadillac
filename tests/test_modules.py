"""Tests for cadillac.modules — real interface extraction."""

import os
import tempfile
import unittest

from cadillac.modules import ModuleSpec, extract_real_interfaces


def _make_mod(path: str = "auth/") -> ModuleSpec:
    return ModuleSpec(
        name=path.rstrip("/").split("/")[-1],
        path=path,
        purpose="",
        files=[],
        exports=[],
        interfaces=[],
        depends_on=[],
        build_order=[],
        test_file="",
    )


class TestRealInterfaceExtraction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, relpath: str, content: str):
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_extracts_function_signatures(self):
        self._write("auth/__init__.py", """
def hash_password(plain: str, salt: str = "") -> str:
    return plain + salt

async def get_user(uid: int) -> dict | None:
    return None
""")
        sigs = extract_real_interfaces(self.workspace, _make_mod("auth/"))
        body = sigs.get("auth/__init__.py", "")
        self.assertIn("def hash_password(plain: str, salt: str='') -> str", body)
        self.assertIn("async def get_user(uid: int) -> dict | None", body)

    def test_extracts_class_signatures(self):
        self._write("auth/__init__.py", """
class User:
    def __init__(self, name: str):
        self.name = name
    def is_admin(self) -> bool:
        return False
""")
        sigs = extract_real_interfaces(self.workspace, _make_mod("auth/"))
        body = sigs["auth/__init__.py"]
        self.assertIn("class User:", body)
        self.assertIn("def __init__(self, name: str):", body)
        self.assertIn("def is_admin(self) -> bool:", body)

    def test_skips_underscore_files(self):
        self._write("auth/__init__.py", "def public(): pass\n")
        self._write("auth/_internal.py", "def secret(): pass\n")
        sigs = extract_real_interfaces(self.workspace, _make_mod("auth/"))
        # Public file present, internal file absent
        self.assertIn("auth/__init__.py", sigs)
        self.assertNotIn("auth/_internal.py", sigs)

    def test_falls_back_silently_on_syntax_error(self):
        self._write("broken/__init__.py", "def oops(:\n    pass\n")
        sigs = extract_real_interfaces(self.workspace, _make_mod("broken/"))
        # Should return empty dict instead of raising
        self.assertEqual(sigs, {})

    def test_returns_empty_for_missing_dir(self):
        sigs = extract_real_interfaces(self.workspace, _make_mod("nonexistent/"))
        self.assertEqual(sigs, {})

    def test_extracts_multiple_files(self):
        self._write("core/__init__.py", "def root(): pass\n")
        self._write("core/models.py", "class Model: pass\n")
        sigs = extract_real_interfaces(self.workspace, _make_mod("core/"))
        self.assertIn("core/__init__.py", sigs)
        self.assertIn("core/models.py", sigs)


if __name__ == "__main__":
    unittest.main()
