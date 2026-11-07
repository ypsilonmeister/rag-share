"""Exercise OAuth HTTP handling and storage without starting an MCP server.

Only MCP registration objects are stubbed; auth, ASGI responses and JSON
storage use the production implementation. No model download is needed.
Run with: python -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.parse


class RegistrationOnlyMCPServer:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self):
        return lambda function: function


def load_server():
    sdk = types.ModuleType("mcp.server.mcpserver")
    sdk.MCPServer = RegistrationOnlyMCPServer
    security = types.ModuleType("mcp.server.transport_security")
    security.TransportSecuritySettings = lambda **kwargs: kwargs
    spec = importlib.util.spec_from_file_location(
        "rag_share_test_server", Path(__file__).resolve().parents[1] / "server.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {
        "mcp": types.ModuleType("mcp"),
        "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.mcpserver": sdk,
        "mcp.server.transport_security": security,
    }):
        spec.loader.exec_module(module)
    return module


server = load_server()


class OAuthRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        overrides = {
            "OAUTH_CLIENTS_PATH": str(root / "clients.json"),
            "OAUTH_TOKENS_PATH": str(root / "tokens.json"),
            "TOKENS_PATH": str(root / "static-tokens.json"),
            "ACCESS_LOG_PATH": str(root / "access.log"),
        }
        paths = patch.multiple(server, **overrides)
        paths.start()
        self.addCleanup(paths.stop)
        server._save_json_file(server.OAUTH_CLIENTS_PATH, {
            "owner": {
                "token_endpoint_auth_method": "client_secret_post",
                "client_secret_hash": server._sha256_hex("correct-secret"),
            },
            "other": {"token_endpoint_auth_method": "none"},
            "public": {"token_endpoint_auth_method": "none"},
        })
        self.access, self.refresh = server._issue_oauth_tokens("owner", "owner")
        self.app = server.OAuthApp()

    def snapshot(self):
        return Path(server.OAUTH_TOKENS_PATH).read_bytes()

    def request(self, **changes):
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh,
            "client_id": "owner",
            "client_secret": "correct-secret",
        }
        form.update(changes)
        form = {key: value for key, value in form.items() if value is not None}

        async def run():
            messages = []

            async def receive():
                return {"type": "http.request", "body":
                        urllib.parse.urlencode(form).encode(), "more_body": False}

            async def send(message):
                messages.append(message)

            await self.app.token({"type": "http"}, receive, send)
            return messages[0]["status"], json.loads(messages[1]["body"])

        return asyncio.run(run())

    def assert_rejected_without_mutation(self, status, error, **changes):
        before = self.snapshot()
        actual_status, body = self.request(**changes)
        self.assertEqual(actual_status, status)
        self.assertEqual(body["error"], error)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(server.verify_oauth_token("Bearer " + self.access), "owner")

    def test_wrong_client_preserves_tokens(self):
        self.assert_rejected_without_mutation(
            400, "invalid_grant", client_id="other", client_secret=None)

    def test_missing_unknown_or_deleted_client_preserves_tokens(self):
        for client_id in (None, "", "unknown"):
            with self.subTest(client_id=client_id):
                self.assert_rejected_without_mutation(
                    401, "invalid_client", client_id=client_id)
        server._save_json_file(server.OAUTH_CLIENTS_PATH, {})
        self.assert_rejected_without_mutation(401, "invalid_client")

    def test_missing_or_incorrect_secret_preserves_tokens(self):
        for secret in (None, "", "wrong-secret"):
            with self.subTest(secret=secret):
                self.assert_rejected_without_mutation(
                    401, "invalid_client", client_secret=secret)

    def test_missing_or_incorrect_refresh_preserves_tokens(self):
        self.assert_rejected_without_mutation(
            400, "invalid_request", refresh_token=None)
        self.assert_rejected_without_mutation(
            400, "invalid_grant", refresh_token="unknown-refresh")

    def test_rotation_replaces_pair_in_one_save_and_rejects_replay(self):
        with patch.object(server, "_save_json_file",
                          wraps=server._save_json_file) as save:
            status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(save.call_count, 1)
        self.assertIsNone(server.verify_oauth_token("Bearer " + self.access))
        self.assertEqual(server.verify_oauth_token(
            "Bearer " + body["access_token"]), "owner")
        records = json.loads(self.snapshot())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[server._sha256_hex(body["access_token"])][
            "refresh_hash"], server._sha256_hex(body["refresh_token"]))
        self.assertNotIn(body["access_token"].encode(), self.snapshot())
        self.assertNotIn(body["refresh_token"].encode(), self.snapshot())
        before = self.snapshot()
        self.assertEqual(self.request()[0], 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.request(refresh_token=body["refresh_token"])[0], 200)

    def test_public_client_can_refresh_without_secret(self):
        access, refresh = server._issue_oauth_tokens("public", "public")
        status, body = self.request(client_id="public", client_secret=None,
                                    refresh_token=refresh)
        self.assertEqual(status, 200)
        self.assertIsNone(server.verify_oauth_token("Bearer " + access))
        self.assertEqual(server.verify_oauth_token(
            "Bearer " + body["access_token"]), "public")
        self.assertEqual(server.verify_oauth_token("Bearer " + self.access), "owner")

    def test_concurrent_refresh_only_succeeds_once(self):
        async def run():
            return await asyncio.gather(
                asyncio.to_thread(self.request), asyncio.to_thread(self.request))

        results = asyncio.run(run())
        self.assertEqual(sorted(status for status, _ in results), [200, 400])
        self.assertEqual(len(json.loads(self.snapshot())), 1)

    def test_pair_generation_failure_preserves_tokens(self):
        before = self.snapshot()
        with patch.object(server, "_new_oauth_token_pair",
                          side_effect=RuntimeError("generation failed")):
            with self.assertRaisesRegex(RuntimeError, "generation failed"):
                self.request()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
