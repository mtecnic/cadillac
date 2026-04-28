"""Unit tests for contracts.py (Phase 2: contracts as first-class artifacts)."""

import json
import os
import tempfile
import unittest

from cadillac.contracts import Contract, Endpoint


class TestContractRoundTrip(unittest.TestCase):
    def test_save_and_load(self):
        c = Contract(
            endpoints=[
                Endpoint(
                    name="register", method="POST", path="/api/auth/register",
                    module="auth", consumed_by=["pages"],
                    request={"username": "string", "email": "string"},
                    response={"201": {"user": "User"}},
                ),
            ],
            types={"User": {"id": "integer"}},
        )
        with tempfile.TemporaryDirectory() as td:
            c.save(td)
            self.assertTrue(os.path.isfile(os.path.join(td, "contracts.json")))
            c2 = Contract.load(td)
            self.assertEqual(len(c2.endpoints), 1)
            self.assertEqual(c2.endpoints[0].method, "POST")
            self.assertEqual(c2.endpoints[0].consumed_by, ["pages"])
            self.assertIn("User", c2.types)

    def test_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            c = Contract.load(td)
            self.assertTrue(c.is_empty())

    def test_from_plan_with_no_contracts_key(self):
        c = Contract.from_plan({"modular": True, "modules": []})
        self.assertTrue(c.is_empty())

    def test_method_normalization(self):
        c = Contract.from_dict({"endpoints": [{"method": "post", "path": "/x"}]})
        self.assertEqual(c.endpoints[0].method, "POST")


class TestModuleFiltering(unittest.TestCase):
    def setUp(self):
        self.c = Contract(endpoints=[
            Endpoint(name="reg", method="POST", path="/api/auth/register",
                     module="auth", consumed_by=["pages"]),
            Endpoint(name="bid", method="POST", path="/api/listings/<id>/bid",
                     module="bids", consumed_by=["pages"]),
            Endpoint(name="admin_purge", method="DELETE", path="/api/admin/purge",
                     module="admin", consumed_by=[]),
        ])

    def test_endpoints_for_module(self):
        self.assertEqual(len(self.c.endpoints_for_module("auth")), 1)
        self.assertEqual(len(self.c.endpoints_for_module("bids")), 1)
        self.assertEqual(len(self.c.endpoints_for_module("nonexistent")), 0)

    def test_endpoints_consumed_by(self):
        self.assertEqual(len(self.c.endpoints_consumed_by("pages")), 2)
        self.assertEqual(len(self.c.endpoints_consumed_by("admin")), 0)


class TestPromptRendering(unittest.TestCase):
    def test_render_for_module_owned(self):
        c = Contract(endpoints=[
            Endpoint(name="reg", method="POST", path="/api/auth/register",
                     module="auth", consumed_by=["pages"],
                     request={"username": "string"}, response={"201": {"user": "User"}}),
        ])
        out = c.render_for_module("auth", kind="backend")
        self.assertIn("POST /api/auth/register", out)
        self.assertIn("username: string", out)
        self.assertIn("201:", out)

    def test_render_for_module_consumed(self):
        c = Contract(endpoints=[
            Endpoint(name="reg", method="POST", path="/api/auth/register",
                     module="auth", consumed_by=["pages"]),
        ])
        out = c.render_for_module("pages", kind="frontend")
        self.assertIn("POST /api/auth/register", out)

    def test_render_for_module_unrelated_is_empty(self):
        c = Contract(endpoints=[
            Endpoint(name="reg", method="POST", path="/api/auth/register",
                     module="auth", consumed_by=["pages"]),
        ])
        # `other` neither owns nor consumes anything; rendered excerpt is empty
        self.assertEqual(c.render_for_module("other"), "")

    def test_render_full(self):
        c = Contract(endpoints=[
            Endpoint(name="reg", method="POST", path="/api/auth/register",
                     module="auth", consumed_by=["pages"]),
        ])
        out = c.render_full()
        self.assertIn("POST /api/auth/register", out)
        self.assertIn("auth", out)
        self.assertIn("pages", out)


class TestContractAlignment(unittest.TestCase):
    """Smoke test for validate.check_contract_alignment with a synthetic project."""

    def _build_synthetic_workspace(self, td: str, *,
                                   backend_route: str | None = None,
                                   frontend_call: str | None = None) -> None:
        os.makedirs(os.path.join(td, "backend", "auth"))
        os.makedirs(os.path.join(td, "frontend", "src", "pages"))
        # backend/__init__-style: a Blueprint declaration + optional route
        with open(os.path.join(td, "backend", "auth", "routes.py"), "w") as f:
            f.write("from flask import Blueprint\n")
            f.write('auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")\n\n')
            if backend_route:
                f.write(f'@auth_bp.route("{backend_route}", methods=["POST"])\n')
                f.write("def handler():\n    return {}\n")
        # frontend
        with open(os.path.join(td, "frontend", "package.json"), "w") as f:
            json.dump({"dependencies": {"react": "*"}}, f)
        with open(os.path.join(td, "frontend", "src", "pages", "Register.tsx"), "w") as f:
            if frontend_call:
                f.write(f'import api from "../api";\napi.post("{frontend_call}", {{}});\n')
            else:
                f.write('// no api calls\n')

    def test_aligned_contract_passes(self):
        from cadillac.validate import check_contract_alignment
        with tempfile.TemporaryDirectory() as td:
            self._build_synthetic_workspace(td, backend_route="/register",
                                            frontend_call="/auth/register")
            Contract(endpoints=[
                Endpoint(name="reg", method="POST", path="/api/auth/register",
                         module="auth", consumed_by=["pages"]),
            ]).save(td)
            results = check_contract_alignment(td, None)
            errors = [r for r in results if not r.passed and r.severity == "error"]
            self.assertEqual(errors, [], f"unexpected errors: {[r.output for r in errors]}")

    def test_missing_backend_route_is_error(self):
        from cadillac.validate import check_contract_alignment
        with tempfile.TemporaryDirectory() as td:
            self._build_synthetic_workspace(td, backend_route=None,
                                            frontend_call="/auth/register")
            Contract(endpoints=[
                Endpoint(name="reg", method="POST", path="/api/auth/register",
                         module="auth", consumed_by=["pages"]),
            ]).save(td)
            results = check_contract_alignment(td, None)
            errors = [r for r in results if not r.passed and r.severity == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("no backend route", errors[0].output)

    def test_missing_frontend_call_is_warning(self):
        from cadillac.validate import check_contract_alignment
        with tempfile.TemporaryDirectory() as td:
            self._build_synthetic_workspace(td, backend_route="/register",
                                            frontend_call=None)
            Contract(endpoints=[
                Endpoint(name="reg", method="POST", path="/api/auth/register",
                         module="auth", consumed_by=["pages"]),
            ]).save(td)
            results = check_contract_alignment(td, None)
            warnings = [r for r in results if not r.passed and r.severity == "warning"]
            errors = [r for r in results if not r.passed and r.severity == "error"]
            self.assertEqual(errors, [])
            self.assertEqual(len(warnings), 1)

    def test_path_param_normalization(self):
        from cadillac.validate import check_contract_alignment
        with tempfile.TemporaryDirectory() as td:
            self._build_synthetic_workspace(
                td,
                backend_route="/listings/<int:id>/bid",
                frontend_call="/listings/${id}/bid",
            )
            Contract(endpoints=[
                Endpoint(name="bid", method="POST", path="/api/auth/listings/<id>/bid",
                         module="auth", consumed_by=["pages"]),
            ]).save(td)
            results = check_contract_alignment(td, None)
            errors = [r for r in results if not r.passed and r.severity == "error"]
            self.assertEqual(errors, [])

    def test_template_literal_dollar_brace_normalizes(self):
        """`${id}` in a frontend call must normalize to <param>, not `$<param>`.

        Regression test: previously the bare `{id}` rule fired before the
        `${id}` rule, leaving a stray `$` and breaking matches.
        """
        from cadillac.validate import _contract_norm_path
        self.assertEqual(_contract_norm_path("/api/x/${id}/y"),
                         _contract_norm_path("/api/x/<id>/y"))
        # And no stray $ in the output:
        self.assertNotIn("$", _contract_norm_path("/api/x/${id}"))

    def test_trailing_slash_normalized(self):
        """Backend routes mounted on `/` (with strict_slashes=False) should
        match a contract path that has no trailing slash."""
        from cadillac.validate import _contract_norm_path
        self.assertEqual(_contract_norm_path("/api/bookmarks/"),
                         _contract_norm_path("/api/bookmarks"))

    def test_apiclient_style_calls_detected(self):
        """Detector must catch TS-idiomatic names: apiClient, bookmarksApi,
        AuthClient — not just bare `api` / `axios`. Including TS generic
        type args between method and paren."""
        from cadillac.validate import _contract_collect_frontend_calls
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "frontend", "src", "services"))
            with open(os.path.join(td, "frontend", "package.json"), "w") as f:
                json.dump({"dependencies": {"react": "*"}}, f)
            with open(os.path.join(td, "frontend", "src", "services", "auth.ts"), "w") as f:
                f.write(
                    'import { apiClient } from "./api";\n'
                    'export function login() {\n'
                    '  return apiClient\n'
                    '    .post<LoginResponse>("/api/auth/login", {});\n'
                    '}\n'
                    'export function listX(client: any) {\n'
                    '  return bookmarksApi.get("/api/bookmarks");\n'
                    '}\n'
                    'export function delX(id: number) {\n'
                    '  return AuthClient.delete(`/api/x/${id}`);\n'
                    '}\n'
                )
            calls = _contract_collect_frontend_calls(td)
            paths = {(m, p) for m, p, _ in calls}
            self.assertIn(("POST", "/api/auth/login"), paths)
            self.assertIn(("GET", "/api/bookmarks"), paths)
            self.assertIn(("DELETE", "/api/x/${id}"), paths)


if __name__ == "__main__":
    unittest.main()
