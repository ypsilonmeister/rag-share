import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import pull


class PullTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.key = self.root / "key.json"
        self.key.write_text("{}")
        overrides = dict(CORPUS=str(self.corpus), KEY_PATH=str(self.key),
                         STATE_PATH=str(self.root / "state.json"), FOLDER_ID="folder")
        paths = patch.multiple(pull, **overrides)
        paths.start()
        self.addCleanup(paths.stop)
        self.old = {"name": "old", "mimeType": "text/plain", "modifiedTime": "1",
                    "webViewLink": "old-link", "text_file": "note.txt"}
        self.current = dict(self.old, id="note", name="new", modifiedTime="2")
        (self.corpus / "note.txt").write_text("old text", encoding="utf-8")
        self.write_manifest({"note": self.old})
        Path(pull.STATE_PATH).write_text(json.dumps({"note": "1"}))

    def write_manifest(self, manifest):
        (self.corpus / "manifest.json").write_text(json.dumps(manifest))

    def manifest(self):
        return json.loads((self.corpus / "manifest.json").read_text())

    def run_pull(self, files=None, result=("ok", b"new text"), error=None,
                 folder_status=200, folder_mime=pull.FOLDER_MIME, trashed=False):
        def collect(token, folder, out):
            out.extend([self.current] if files is None else files)

        with patch.object(pull, "get_access_token", return_value="test"), \
             patch.object(pull, "collect", side_effect=collect), \
             patch.object(pull, "api_get", return_value=Mock(
                 status_code=folder_status, json=lambda: {
                     "id": "folder", "mimeType": folder_mime, "trashed": trashed})), \
             patch.object(pull, "extract", return_value=result, side_effect=error):
            return pull.main()

    def test_transient_failure_retains_old_version_and_recovers(self):
        self.assertEqual(self.run_pull(result=("error-http-503", None)), 1)
        retained = self.manifest()["note"]
        self.assertEqual(retained["modifiedTime"], "1")
        self.assertEqual(retained["name"], "old")
        self.assertTrue(retained["stale"])
        self.assertEqual((self.corpus / "note.txt").read_text(), "old text")
        self.assertEqual(json.loads(Path(pull.STATE_PATH).read_text())["note"], "1")
        self.assertEqual(self.run_pull(), 0)
        self.assertNotIn("stale", self.manifest()["note"])
        self.assertEqual(self.manifest()["note"]["modifiedTime"], "2")

    def test_timeout_retains_previous_copy(self):
        self.assertEqual(self.run_pull(error=requests.Timeout()), 1)
        self.assertTrue(self.manifest()["note"]["stale"])

    def test_unsupported_format_does_not_retain_stale_copy(self):
        self.assertEqual(self.run_pull(result=("skip-mime", None)), 0)
        self.assertNotIn("note", self.manifest())

    def test_removed_file_is_removed_while_another_file_remains(self):
        self.assertEqual(self.run_pull(files=[dict(self.current, id="another")]), 0)
        self.assertNotIn("note", self.manifest())
        self.assertFalse((self.corpus / "note.txt").exists())

    def test_failed_new_file_has_no_fake_manifest_entry(self):
        self.assertEqual(self.run_pull(files=[dict(self.current, id="new")],
                                       result=("error-http-429", None)), 1)
        self.assertNotIn("new", self.manifest())

    def test_verified_empty_folder_removes_last_memory(self):
        self.assertEqual(self.run_pull(files=[]), 0)
        self.assertEqual(self.manifest(), {})
        self.assertEqual(json.loads(Path(pull.STATE_PATH).read_text()), {})
        self.assertFalse((self.corpus / "note.txt").exists())

    def test_unverified_empty_folder_preserves_corpus(self):
        for changes in ({"folder_status": 403}, {"folder_status": 404},
                        {"folder_status": 503}, {"folder_mime": "text/plain"},
                        {"trashed": True}):
            with self.subTest(changes=changes):
                with self.assertRaises(RuntimeError):
                    self.run_pull(files=[], **changes)
                self.assertEqual(self.manifest(), {"note": self.old})
                self.assertEqual((self.corpus / "note.txt").read_text(), "old text")
