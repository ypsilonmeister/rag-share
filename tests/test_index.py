import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from test_oauth_refresh import server


class IndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        paths = patch.multiple(server, CORPUS_DIR=str(self.root),
                               MANIFEST_PATH=str(self.root / "manifest.json"))
        paths.start()
        self.addCleanup(paths.stop)
        self.index = server.Index()
        self.embed = patch.object(self.index, "_embed", side_effect=self.vectors)
        self.embedding = self.embed.start()
        self.addCleanup(self.embed.stop)

    @staticmethod
    def vectors(texts):
        vectors = np.zeros((len(texts), 768), dtype=np.float32)
        vectors[:, 0] = 1
        return vectors

    def write(self, manifest, texts=None):
        for filename, text in (texts or {}).items():
            (self.root / filename).write_text(text, encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Avoid depending on filesystem timestamp granularity in fixtures.
        self.index.manifest_mtime = None

    def test_read_all_pages_including_match_after_snippet(self):
        text = "前半" * 500 + "検索対象の重要な規約" + "後半" * 2000
        self.write({"note": {"text_file": "note.txt", "name": "guide",
                             "modifiedTime": "1"}}, {"note.txt": text})
        result = self.index.search("重要な規約", 5)
        file_id = result[0]["fileId"]
        offset, pages = 0, []
        while offset is not None:
            page = self.index.read(file_id, offset, 201)
            self.assertLessEqual(len(page["text"]), 201)
            pages.append(page["text"])
            offset = page["next_offset"]
        self.assertEqual("".join(pages), text)

    def test_unknown_file_and_path_inputs_are_rejected(self):
        for file_id in ("missing", "../server.py", str(self.root / "secret.txt")):
            with self.subTest(file_id=file_id), self.assertRaises(ValueError):
                self.index.read(file_id)

    def test_page_bounds(self):
        self.write({"note": {"text_file": "note.txt"}}, {"note.txt": "abc"})
        for offset, limit in ((-1, 10), (0, 0), (0, 8001)):
            with self.assertRaises(ValueError):
                self.index.read("note", offset, limit)
        self.assertEqual(self.index.read("note", 20)["text"], "")
        self.assertIsNone(self.index.read("note", 20)["next_offset"])

    def test_deleted_memory_cannot_be_read(self):
        self.write({"note": {"text_file": "note.txt"}}, {"note.txt": "abc"})
        self.assertEqual(self.index.read("note")["text"], "abc")
        self.write({})
        with self.assertRaises(ValueError):
            self.index.read("note")

    def test_correction_is_attached_outside_top_k_and_history_is_retained(self):
        old = '---\nid: original\ndate: 2025-01-01\nstatus: active\n---\n旧決定の固有語'
        new = ('---\nid: correction\ndate: 2026-01-01\nstatus: active\n'
               'supersedes: ["original"]\n---\n新しい方針')
        self.write({fid: {"text_file": fid + ".txt", "name": fid}
                    for fid in ("old", "new")}, {"old.txt": old, "new.txt": new})
        hit = self.index.search("旧決定の固有語", 1)[0]
        self.assertEqual(hit["fileId"], "old")
        self.assertEqual(hit["superseded_by"][0]["fileId"], "new")
        self.assertEqual(hit["superseded_by"][0]["record"]["date"], "2026-01-01")
        self.assertEqual(self.index.read("old")["text"], old)
        self.assertEqual(self.index.read("new")["text"], new)
        self.assertEqual(self.index.read("old")["superseded_by"], hit["superseded_by"])

    def test_correction_cycles_and_duplicate_ids_are_safe(self):
        files = {
            "a": {"meta": {}, "record": {"id": "A", "supersedes": ["B"]}},
            "b": {"meta": {}, "record": {"id": "B", "supersedes": ["A"]}},
        }
        self.assertEqual(len(server.record_context("a", files)["superseded_by"]), 1)
        files["duplicate"] = {"meta": {}, "record": {"id": "A"}}
        self.assertEqual(server.record_context("a", files)["superseded_by"], [])

    def test_legacy_and_invalid_front_matter(self):
        for text in ("legacy text", "---\nid: [\n---\ntext", "---\n- list\n---\ntext"):
            self.assertEqual(server.memory_metadata(text), {})
