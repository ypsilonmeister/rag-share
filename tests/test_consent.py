import asyncio
import unittest
from unittest.mock import patch
import urllib.parse

from test_oauth_refresh import server


class ConsentTests(unittest.TestCase):
    def test_passphrases(self):
        cases = [
            ("ascii-secret", "ascii-secret", 302),
            ("ascii-secret", "wrong", 200),
            ("日本語の合言葉", "日本語の合言葉", 302),
            ("日本語の合言葉", "別の合言葉", 200),
            ("ascii-secret", "不正な入力", 200),
            (None, "anything", 503),
        ]
        for expected, provided, status in cases:
            with self.subTest(expected=expected, provided=provided):
                form = {
                    "client_id": "client", "redirect_uri": "https://example.com/cb",
                    "response_type": "code", "code_challenge": "challenge",
                    "passphrase": provided,
                }

                async def run():
                    messages = []

                    async def receive():
                        return {"type": "http.request", "body":
                                urllib.parse.urlencode(form).encode()}

                    async def send(message):
                        messages.append(message)

                    await server.OAuthApp().authorize_post({}, receive, send)
                    return messages

                with patch.object(server.OAuthApp, "_validate_authorize",
                                  return_value=({"client_name": "test"}, None)), \
                     patch.object(server, "_consent_passphrase", return_value=expected), \
                     patch.object(server, "access_log"), \
                     patch.object(server, "_auth_codes", {}):
                    messages = asyncio.run(run())
                    self.assertEqual(messages[0]["status"], status)
                    self.assertEqual(len(server._auth_codes), int(status == 302))
