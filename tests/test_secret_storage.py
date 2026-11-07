import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_oauth_refresh import server


class SecretStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "tokens.json"
        self.path.write_text('{"existing": {"label": "owner"}}')

    def test_replace_and_fsync_failures_preserve_last_good_file(self):
        before = self.path.read_bytes()
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation):
                with patch.object(server.os, operation, side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        server._save_json_file(str(self.path), {"new": {}})
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_successful_write_is_complete_and_private(self):
        server._save_json_file(str(self.path), {"new": "日本語"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"new": "日本語"})
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_or_wrong_type_state_is_not_silently_overwritten(self):
        for broken in ('{"truncated":', '[]'):
            with self.subTest(broken=broken):
                self.path.write_text(broken)
                with patch.object(server, "OAUTH_TOKENS_PATH", str(self.path)):
                    with self.assertRaises(ValueError):
                        server._issue_oauth_tokens("client", "owner")
                    self.assertIsNone(server.verify_oauth_token("Bearer test"))
                self.assertEqual(self.path.read_text(), broken)

    def test_missing_state_can_be_initialized(self):
        missing = str(self.root / "new.json")
        self.assertEqual(server._load_json_file(missing, {}), {})
