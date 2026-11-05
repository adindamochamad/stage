"""Unit tests for the scratch-copy engine. Run: python3 tests/test_engine.py"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine.py"
spec = importlib.util.spec_from_file_location("stage_engine", ENGINE_PATH)
engine = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["stage_engine"] = engine
spec.loader.exec_module(engine)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.storage = self.root / "data"
        self.workspace.mkdir()
        self.note = self.workspace / "note.txt"
        self.note.write_text("OLD\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_keeps_original_and_apply_updates_it(self):
        directive = engine.plan_redirect(
            "note.txt", "write_file", "task", workspace=self.workspace, storage=self.storage,
        )
        self.assertEqual(directive["action"], "modify")
        scratch = Path(directive["args"]["path"])
        scratch.write_text("NEW\n", encoding="utf-8")
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")
        status = engine.stage_status("task", storage=self.storage)
        self.assertEqual(status["pending"][0]["path"], "note.txt")
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(applied["applied"], ["note.txt"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "NEW\n")
        self.assertEqual(engine.stage_status("task", storage=self.storage)["pending"], [])

    def test_patch_copies_then_discard_leaves_original(self):
        directive = engine.plan_redirect(
            str(self.note), "patch", "task", workspace=self.workspace, storage=self.storage,
        )
        scratch = Path(directive["args"]["path"])
        self.assertEqual(scratch.read_text(encoding="utf-8"), "OLD\n")
        scratch.write_text("EDITED\n", encoding="utf-8")
        discarded = engine.stage_discard("task", storage=self.storage)
        self.assertEqual(discarded["discarded"], ["note.txt"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")
        self.assertFalse(scratch.exists())
        self.assertEqual(engine.stage_status("task", storage=self.storage)["pending"], [])

    def test_patch_of_missing_file_is_blocked(self):
        directive = engine.plan_redirect(
            "missing.txt", "patch", "task", workspace=self.workspace, storage=self.storage,
        )
        self.assertEqual(directive["action"], "block")
        self.assertIn("existing file", directive["message"])

    def test_outside_workspace_is_blocked(self):
        outside = self.root / "outside.txt"
        directive = engine.plan_redirect(
            str(outside), "write_file", "task", workspace=self.workspace, storage=self.storage,
        )
        self.assertEqual(directive["action"], "block")
        self.assertIn("outside", directive["message"])
        self.assertFalse(outside.exists())

    def test_apply_with_nothing_pending_changes_nothing(self):
        result = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(result, {"applied": [], "errors": []})
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")

    def test_other_tools_are_ignored(self):
        self.assertIsNone(engine.plan_redirect("note.txt", "read_file", "task", workspace=self.workspace, storage=self.storage))


if __name__ == "__main__":
    unittest.main()
