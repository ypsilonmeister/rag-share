import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_oauth_refresh import server


class AccessLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "access.log"
        settings = patch.multiple(server, ACCESS_LOG_PATH=str(self.path), LOG_QUERIES=False,
                                  LOG_MAX_BYTES=200, LOG_BACKUPS=2)
        settings.start()
        self.addCleanup(settings.stop)

    def test_query_is_omitted_unless_explicitly_enabled(self):
        server.access_log("owner", "memory_search", "private-search", 1)
        self.assertNotIn("private-search", self.path.read_text())
        with patch.object(server, "LOG_QUERIES", True):
            server.access_log("owner", "memory_search", "opt-in-search", 1)
        self.assertIn("opt-in-search", self.path.read_text())

    def test_rotation_is_bounded_and_private(self):
        for number in range(30):
            server.access_log("owner", "read", str(number) * 20, 1)
        files = list(self.root.iterdir())
        self.assertEqual(len(files), 3)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 200)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_user_controlled_fields_cannot_inject_log_rows(self):
        server.access_log("name\r\n\tforged", "register", "detail\r\n\tforged", 0)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].split("\t")), 5)
