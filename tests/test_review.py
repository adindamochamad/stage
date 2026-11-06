"""Review-path tests: diff, per-path apply, shell gate, receipts, V4A. Run: python3 tests/test_review.py"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine.py"
spec = importlib.util.spec_from_file_location("stage_engine", ENGINE_PATH)
engine = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["stage_engine_review"] = engine
spec.loader.exec_module(engine)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.storage = self.root / "data"
        self.workspace.mkdir()
        self.note = self.workspace / "note.txt"
        self.other = self.workspace / "other.txt"
        self.note.write_text("OLD\n", encoding="utf-8")
        self.other.write_text("KEEP\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, relative: str, content: str, task: str = "task") -> Path:
        directive = engine.plan_redirect(
            relative, "write_file", task, workspace=self.workspace, storage=self.storage,
        )
        scratch = Path(directive["args"]["path"])
        scratch.write_text(content, encoding="utf-8")
        return scratch

    def test_status_includes_added_removed_conflict(self):
        self._write("note.txt", "NEW LINE\n")
        status = engine.stage_status("task", storage=self.storage)
        row = status["pending"][0]
        self.assertEqual(row["path"], "note.txt")
        self.assertFalse(row["conflict"])
        self.assertGreaterEqual(row["added"], 1)
        self.assertGreaterEqual(row["removed"], 1)

    def test_diff_shows_unified_and_new_file_from_empty(self):
        self._write("note.txt", "NEW\n")
        self._write("fresh.txt", "CREATED\n")
        diff = engine.stage_diff("task", workspace=self.workspace, storage=self.storage)
        by_path = {row["path"]: row for row in diff["files"]}
        self.assertIn("NEW", by_path["note.txt"]["diff"])
        self.assertIn("OLD", by_path["note.txt"]["diff"])
        self.assertFalse(by_path["note.txt"]["conflict"])
        self.assertIn("CREATED", by_path["fresh.txt"]["diff"])
        self.assertFalse((self.workspace / "fresh.txt").exists())

    def test_apply_one_path_leaves_the_other_staged(self):
        self._write("note.txt", "NOTE\n")
        self._write("other.txt", "OTHER\n")
        result = engine.stage_apply(
            "task", ["note.txt"], workspace=self.workspace, storage=self.storage,
        )
        self.assertEqual(result["applied"], ["note.txt"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "NOTE\n")
        self.assertEqual(self.other.read_text(encoding="utf-8"), "KEEP\n")
        pending = [row["path"] for row in engine.stage_status("task", storage=self.storage)["pending"]]
        self.assertEqual(pending, ["other.txt"])

    def test_discard_one_path_leaves_the_other_staged(self):
        self._write("note.txt", "NOTE\n")
        self._write("other.txt", "OTHER\n")
        result = engine.stage_discard(
            "task", ["note.txt"], workspace=self.workspace, storage=self.storage,
        )
        self.assertEqual(result["discarded"], ["note.txt"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")
        pending = [row["path"] for row in engine.stage_status("task", storage=self.storage)["pending"]]
        self.assertEqual(pending, ["other.txt"])

    def test_unknown_path_is_an_error_and_does_not_apply_the_rest(self):
        self._write("note.txt", "NOTE\n")
        result = engine.stage_apply(
            "task", ["missing.txt"], workspace=self.workspace, storage=self.storage,
        )
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["errors"][0]["path"], "missing.txt")
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")
        self.assertEqual(
            engine.stage_status("task", storage=self.storage)["pending"][0]["path"], "note.txt",
        )

    def test_apply_receipt_has_before_and_after_hashes(self):
        before = _sha(self.note)
        self._write("note.txt", "NEW\n")
        result = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        after = _sha(self.note)
        self.assertEqual(result["applied"], ["note.txt"])
        receipt = result["receipts"][0]
        self.assertEqual(receipt["path"], "note.txt")
        self.assertEqual(receipt["before_sha256"], before)
        self.assertEqual(receipt["after_sha256"], after)
        listed = engine.stage_receipts(storage=self.storage)
        self.assertEqual(listed["receipts"][-1]["after_sha256"], after)

    def test_v4a_update_does_not_touch_original_until_apply(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NEW\n"
            "*** End Patch\n"
        )
        directive = engine.plan_tool(
            "patch",
            {"mode": "patch", "patch": patch},
            "task",
            workspace=self.workspace,
            storage=self.storage,
            tool_call_id="upd",
        )
        self.assertEqual(directive["action"], "modify")
        rewritten = directive["args"]["patch"]
        self.assertIn(str(self.storage), rewritten)
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")
        pending = engine.stage_status("task", storage=self.storage)["pending"]
        self.assertEqual(pending[0]["path"], "note.txt")
        self.assertFalse(pending[0]["committed"])
        engine.commit_v4a_batch("task", storage=self.storage, tool_call_id="upd")
        status = engine.stage_status("task", storage=self.storage)
        self.assertEqual(status["pending"][0]["path"], "note.txt")
        self.assertTrue(status["pending"][0]["committed"])
        scratch_path = Path(
            engine._load(engine._journal_path(self.storage, "task"))["files"]["note.txt"]["scratch"]
        )
        self.assertEqual(scratch_path.read_text(encoding="utf-8"), "OLD\n")
        scratch_path.write_text("NEW\n", encoding="utf-8")
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(applied["applied"], ["note.txt"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "NEW\n")

    def test_empty_paths_does_not_apply_everything(self):
        self._write("note.txt", "NOTE\n")
        result = engine.stage_apply(
            "task", [], workspace=self.workspace, storage=self.storage,
        )
        self.assertEqual(result["applied"], [])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")

    def test_v4a_delete_does_not_remove_original_until_apply(self):
        patch = (
            "*** Begin Patch\n"
            "*** Delete File: note.txt\n"
            "*** End Patch\n"
        )
        directive = engine.plan_tool(
            "patch",
            {"mode": "patch", "patch": patch},
            "task",
            workspace=self.workspace,
            storage=self.storage,
        )
        self.assertEqual(directive["action"], "block")
        self.assertIn("deletion is staged", directive["message"])
        self.assertTrue(self.note.is_file())
        status = engine.stage_status("task", storage=self.storage)
        self.assertEqual(status["pending"][0]["op"], "delete")
        diff = engine.stage_diff("task", ["note.txt"], workspace=self.workspace, storage=self.storage)
        self.assertIn("-OLD", diff["files"][0]["diff"])
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(applied["applied"], ["note.txt"])
        self.assertFalse(self.note.exists())
        self.assertIsNone(applied["receipts"][0]["after_sha256"])

    def test_v4a_mixed_delete_is_not_applyable_until_commit(self):
        gone = self.workspace / "gone.txt"
        gone.write_text("BYE\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Delete File: gone.txt\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NEW\n"
            "*** End Patch\n"
        )
        directive = engine.plan_tool(
            "patch",
            {"mode": "patch", "patch": patch},
            "task",
            workspace=self.workspace,
            storage=self.storage,
        )
        self.assertEqual(directive["action"], "modify")
        self.assertTrue(gone.is_file())
        self.assertEqual(gone.read_text(encoding="utf-8"), "BYE\n")
        pending = {row["path"]: row for row in engine.stage_status("task", storage=self.storage)["pending"]}
        self.assertFalse(pending["gone.txt"]["committed"])
        self.assertFalse(pending["note.txt"]["committed"])
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(applied["applied"], [])
        self.assertTrue(gone.is_file())
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")

    def test_v4a_mixed_drop_removes_uncommitted_batch(self):
        gone = self.workspace / "gone.txt"
        gone.write_text("BYE\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Delete File: gone.txt\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NEW\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch",
            {"mode": "patch", "patch": patch},
            "task",
            workspace=self.workspace,
            storage=self.storage,
            tool_call_id="mix-drop",
        )
        dropped = engine.drop_v4a_batch("task", storage=self.storage, tool_call_id="mix-drop")
        self.assertIn("gone.txt", dropped["dropped"])
        self.assertIn("note.txt", dropped["dropped"])
        self.assertTrue(gone.is_file())
        self.assertEqual(gone.read_text(encoding="utf-8"), "BYE\n")
        self.assertEqual(engine.stage_status("task", storage=self.storage)["pending"], [])
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(applied["applied"], [])
        self.assertTrue(gone.is_file())

    def test_v4a_mixed_commit_then_apply_deletes(self):
        gone = self.workspace / "gone.txt"
        gone.write_text("BYE\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Delete File: gone.txt\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NEW\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch",
            {"mode": "patch", "patch": patch},
            "task",
            workspace=self.workspace,
            storage=self.storage,
            tool_call_id="mix-commit",
        )
        scratch = Path(
            engine._load(engine._journal_path(self.storage, "task"))["files"]["note.txt"]["scratch"]
        )
        scratch.write_text("NEW\n", encoding="utf-8")
        engine.commit_v4a_batch("task", storage=self.storage, tool_call_id="mix-commit")
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(sorted(applied["applied"]), ["gone.txt", "note.txt"])
        self.assertFalse(gone.exists())
        self.assertEqual(self.note.read_text(encoding="utf-8"), "NEW\n")

    def test_v4a_outside_workspace_blocks_the_whole_call(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/outside.txt\n"
            "*** End Patch\n"
        )
        directive = engine.plan_tool(
            "patch",
            {"mode": "patch", "patch": patch},
            "task",
            workspace=self.workspace,
            storage=self.storage,
        )
        self.assertEqual(directive["action"], "block")
        self.assertEqual(engine.stage_status("task", storage=self.storage)["pending"], [])

    def test_shell_blocks_mutations_and_allows_reads(self):
        allowed = [
            "ls",
            "cat note.txt",
            "git status",
            "git diff",
            "python3 tests/test_engine.py",
            "pytest",
            "grep 'a>b' README.md",
            "ls >/dev/null",
            "cp note.txt /tmp/backup",
            "echo x 2>&1",
            "cmd 2>&1",
            "echo x >&1",
            "echo x >&-",
            "echo x >/dev/null",
        ]
        blocked = [
            "sed -i s/a/b/ note.txt",
            "perl -i -pe s/a/b/ note.txt",
            "tee note.txt",
            "truncate -s 0 note.txt",
            "echo hi > note.txt",
            "echo hi >> note.txt",
            "echo NEW>note.txt",
            "echo NEW> note.txt",
            'echo NEW>"note.txt"',
            ">note.txt",
            "echo x->note.txt",
            'echo x >"note.txt" /dev/null',
            "echo x >&note.txt",
            'echo x >"note".txt',
            "echo x >1",
            "echo x >-",
            'echo x >"&1"',
            "rm note.txt",
            "mv note.txt gone.txt",
            "mv note.txt /tmp/x",
            "cp note.txt copy.txt",
            "cp /tmp/x note.txt",
            "git apply changes.patch",
            "git checkout note.txt",
            "git restore note.txt",
            "git reset --hard",
        ]
        for command in allowed:
            result = engine.plan_command(
                "terminal", {"command": command}, workspace=self.workspace,
            )
            self.assertIsNone(result, command)
        for command in blocked:
            result = engine.plan_command(
                "terminal", {"command": command}, workspace=self.workspace,
            )
            self.assertEqual(result["action"], "block", command)
            self.assertIn("write_file or patch", result["message"])

    def test_rm_outside_workspace_is_allowed(self):
        result = engine.plan_command(
            "terminal", {"command": "rm /tmp/stage-outside-file"}, workspace=self.workspace,
        )
        self.assertIsNone(result)

    def test_execute_code_blocks_literal_writes(self):
        blocked = [
            "open('note.txt', 'w').write('x')",
            "Path('note.txt').write_text('x')",
            "Path('note.txt').write_bytes(b'x')",
            "Path('note.txt').unlink()",
            "os.remove('note.txt')",
            "subprocess.run(['rm', 'note.txt'])",
            "os.system('rm note.txt')",
        ]
        for code in blocked:
            result = engine.plan_command(
                "execute_code", {"code": code}, workspace=self.workspace,
            )
            self.assertEqual(result["action"], "block", code)
        allowed = engine.plan_command(
            "execute_code",
            {"code": "print(open('note.txt').read())"},
            workspace=self.workspace,
        )
        self.assertIsNone(allowed)

    def test_failed_v4a_restores_earlier_staged_edit(self):
        self._write("note.txt", "STAGED\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NEW\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch",
            {"mode": "patch", "patch": patch},
            "task",
            workspace=self.workspace,
            storage=self.storage,
            tool_call_id="v4a-fail",
        )
        dropped = engine.drop_v4a_batch("task", storage=self.storage, tool_call_id="v4a-fail")
        self.assertEqual(dropped["dropped"], ["note.txt"])
        pending = engine.stage_status("task", storage=self.storage)["pending"]
        self.assertEqual(pending[0]["path"], "note.txt")
        self.assertTrue(pending[0]["committed"])
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(applied["applied"], ["note.txt"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "STAGED\n")

    def test_rollback_then_next_patch_keeps_staged_bytes(self):
        self._write("note.txt", "STAGED\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NEW\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="v4a-1",
        )
        engine.drop_v4a_batch("task", storage=self.storage, tool_call_id="v4a-1")
        directive = engine.plan_redirect(
            "note.txt", "patch", "task", workspace=self.workspace, storage=self.storage,
        )
        scratch = Path(directive["args"]["path"])
        self.assertEqual(scratch.read_text(encoding="utf-8"), "STAGED\n")
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")

    def test_two_v4a_batches_settle_by_tool_call_id(self):
        patch_note = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NOTE\n"
            "*** End Patch\n"
        )
        patch_other = (
            "*** Begin Patch\n"
            "*** Update File: other.txt\n"
            "@@\n"
            "-KEEP\n"
            "+OTHER\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_note}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="call-a",
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_other}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="call-b",
        )
        engine.drop_v4a_batch("task", storage=self.storage, tool_call_id="call-a")
        engine.commit_v4a_batch("task", storage=self.storage, tool_call_id="call-a")
        pending = {row["path"]: row for row in engine.stage_status("task", storage=self.storage)["pending"]}
        self.assertFalse(pending["other.txt"]["committed"])
        engine.commit_v4a_batch("task", storage=self.storage, tool_call_id="call-b")
        pending = {row["path"]: row for row in engine.stage_status("task", storage=self.storage)["pending"]}
        self.assertNotIn("note.txt", pending)
        self.assertTrue(pending["other.txt"]["committed"])
        scratch = Path(
            engine._load(engine._journal_path(self.storage, "task"))["files"]["other.txt"]["scratch"]
        )
        scratch.write_text("OTHER\n", encoding="utf-8")
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(applied["applied"], ["other.txt"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "OLD\n")
        self.assertEqual(self.other.read_text(encoding="utf-8"), "OTHER\n")

    def test_structured_success_does_not_look_failed(self):
        result = json.dumps({"success": True, "diff": 'print("error")'})
        self.assertFalse(engine.patch_call_failed("ok", "", result))
        self.assertTrue(engine.patch_call_failed("error", "", result))
        self.assertFalse(engine.patch_call_failed("ok", "", 'success:true\nprint("error")'))

    def test_second_callback_does_not_commit_other_batch(self):
        patch_note = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NOTE\n"
            "*** End Patch\n"
        )
        patch_other = (
            "*** Begin Patch\n"
            "*** Update File: other.txt\n"
            "@@\n"
            "-KEEP\n"
            "+OTHER\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_note}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="call-a",
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_other}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="call-b",
        )
        engine.commit_v4a_batch("task", storage=self.storage, tool_call_id="call-a")
        engine.commit_v4a_batch("task", storage=self.storage, tool_call_id="call-a")
        pending = {row["path"]: row for row in engine.stage_status("task", storage=self.storage)["pending"]}
        self.assertTrue(pending["note.txt"]["committed"])
        self.assertFalse(pending["other.txt"]["committed"])

    def test_prior_backup_does_not_collide_with_workspace_name(self):
        sibling = self.workspace / "note.txt.prior"
        sibling.write_text("SIBLING\n", encoding="utf-8")
        self._write("note.txt", "STAGED\n")
        self._write("note.txt.prior", "KEEP PRIOR FILE\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+NEW\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="collide",
        )
        engine.drop_v4a_batch("task", storage=self.storage, tool_call_id="collide")
        applied = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(sorted(applied["applied"]), ["note.txt", "note.txt.prior"])
        self.assertEqual(self.note.read_text(encoding="utf-8"), "STAGED\n")
        self.assertEqual(sibling.read_text(encoding="utf-8"), "KEEP PRIOR FILE\n")

    def test_v4a_overlap_on_same_path_is_blocked(self):
        patch_a = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+A\n"
            "*** End Patch\n"
        )
        patch_b = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+B\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_a}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-a",
        )
        before = engine._load(engine._journal_path(self.storage, "task"))
        blocked = engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_b}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-b",
        )
        self.assertEqual(blocked["action"], "block")
        self.assertIn("belum selesai", blocked["message"])
        after = engine._load(engine._journal_path(self.storage, "task"))
        self.assertEqual(after, before)
        pending = engine.stage_status("task", storage=self.storage)["pending"]
        self.assertEqual([row["path"] for row in pending], ["note.txt"])
        self.assertFalse(pending[0]["committed"])

    def test_v4a_overlap_on_other_path_is_allowed(self):
        a_file = self.workspace / "a.txt"
        b_file = self.workspace / "b.txt"
        a_file.write_text("A0\n", encoding="utf-8")
        b_file.write_text("B0\n", encoding="utf-8")
        patch_a = (
            "*** Begin Patch\n"
            "*** Update File: a.txt\n"
            "@@\n"
            "-A0\n"
            "+A1\n"
            "*** End Patch\n"
        )
        patch_b = (
            "*** Begin Patch\n"
            "*** Update File: b.txt\n"
            "@@\n"
            "-B0\n"
            "+B1\n"
            "*** End Patch\n"
        )
        first = engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_a}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-a",
        )
        self.assertEqual(first["action"], "modify")
        second = engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_b}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-b",
        )
        self.assertEqual(second["action"], "modify")
        pending = {row["path"]: row for row in engine.stage_status("task", storage=self.storage)["pending"]}
        self.assertFalse(pending["a.txt"]["committed"])
        self.assertFalse(pending["b.txt"]["committed"])

    def test_v4a_overlap_allowed_after_commit_or_drop(self):
        patch_a = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+A\n"
            "*** End Patch\n"
        )
        patch_b = (
            "*** Begin Patch\n"
            "*** Update File: note.txt\n"
            "@@\n"
            "-OLD\n"
            "+B\n"
            "*** End Patch\n"
        )
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_a}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-a",
        )
        engine.commit_v4a_batch("task", storage=self.storage, tool_call_id="batch-a")
        after_commit = engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_b}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-b",
        )
        self.assertEqual(after_commit["action"], "modify")
        engine.drop_v4a_batch("task", storage=self.storage, tool_call_id="batch-b")
        engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_a}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-c",
        )
        engine.drop_v4a_batch("task", storage=self.storage, tool_call_id="batch-c")
        after_drop = engine.plan_tool(
            "patch", {"mode": "patch", "patch": patch_b}, "task",
            workspace=self.workspace, storage=self.storage, tool_call_id="batch-d",
        )
        self.assertEqual(after_drop["action"], "modify")


if __name__ == "__main__":
    unittest.main()
