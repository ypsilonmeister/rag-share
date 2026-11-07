import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_oauth_refresh import server


class OAuthLimitsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = patch.multiple(
            server, OAUTH_CLIENTS_PATH=str(self.root / "clients.json"),
            ACCESS_LOG_PATH=str(self.root / "access.log"), OAUTH_MAX_CLIENTS=2,
            OAUTH_BODY_LIMIT=128,
            OAUTH_RATE_LIMITS={"/register": 2, "/authorize": 2, "/token": 2})
        settings.start()
        self.addCleanup(settings.stop)
        self.app = server.OAuthApp()

    def request(self, path="/register", chunks=None, headers=None):
        if chunks is None:
            chunks = [json.dumps({"redirect_uris": ["https://example.com/cb"]}).encode()]

        async def run():
            messages = []
            remaining = list(chunks)

            async def receive():
                chunk = remaining.pop(0)
                return {"type": "http.request", "body": chunk,
                        "more_body": bool(remaining)}

            async def send(message):
                messages.append(message)

            await self.app({"method": "POST", "path": path,
                            "headers": headers or []}, receive, send)
            content_type = dict(messages[0]["headers"])[b"content-type"]
            body = (json.loads(messages[1]["body"]) if b"application/json" in content_type
                    else messages[1]["body"].decode())
            return messages[0], body

        return asyncio.run(run())

    def test_limits_apply_to_all_public_post_routes_and_reset(self):
        for path in ("/register", "/authorize", "/token"):
            with self.subTest(path=path), patch.object(server.time, "monotonic", return_value=0):
                self.assertNotEqual(self.request(path)[0]["status"], 429)
                self.assertNotEqual(self.request(path)[0]["status"], 429)
                response, body = self.request(path, headers=[(b"x-forwarded-for", b"spoofed")])
                self.assertEqual(response["status"], 429)
                self.assertIn(b"retry-after", dict(response["headers"]))
                self.assertEqual(body["error"], "rate_limit_exceeded")
        with patch.object(server.time, "monotonic", return_value=61):
            self.assertNotEqual(self.request("/token")[0]["status"], 429)

    def test_streamed_oversize_body_is_rejected_before_registration(self):
        response, body = self.request(chunks=[b"x" * 100, b"x" * 29])
        self.assertEqual(response["status"], 413)
        self.assertEqual(body["error"], "request_too_large")
        self.assertFalse(Path(server.OAUTH_CLIENTS_PATH).exists())

    def test_client_cap_and_owner_cleanup(self):
        with patch.object(server, "OAUTH_RATE_LIMITS", {"/register": 100}):
            self.assertEqual(self.request()[0]["status"], 201)
            self.assertEqual(self.request()[0]["status"], 201)
            before = Path(server.OAUTH_CLIENTS_PATH).read_bytes()
            response, body = self.request()
            self.assertEqual(response["status"], 429)
            self.assertEqual(body["error"], "registration_limit_reached")
            self.assertEqual(Path(server.OAUTH_CLIENTS_PATH).read_bytes(), before)
            clients = json.loads(before)
            del clients[next(iter(clients))]
            server._save_json_file(server.OAUTH_CLIENTS_PATH, clients)
            self.assertEqual(self.request()[0]["status"], 201)

    def test_json_array_is_rejected_as_metadata(self):
        self.assertEqual(self.request(chunks=[b"[]"])[0]["status"], 400)
