"""Tests for Hermes write protection integration. Run: python3 tests/test_protections.py"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ENGINE_PATH = Path(__file__).resolve().parents[1] / "engine.py"
spec = importlib.util.spec_from_file_location("stage_engine", ENGINE_PATH)
engine = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["stage_engine"] = engine
spec.loader.exec_module(engine)


class ProtectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.storage = self.root / "data"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_redirect_returns_none_for_protected_paths(self):
        """plan_redirect should return None for paths Hermes would deny, letting core handle them."""
        
        def mock_get_write_protection_error(path: str) -> str | None:
            # Simulate Hermes denying ~/.ssh/authorized_keys
            if "authorized_keys" in path or ".ssh" in path:
                return "Write denied: '~/.ssh/authorized_keys' is a protected system/credential file."
            return None
        
        # Mock the internal protection check function
        with patch.object(engine, "_get_write_protection_error", side_effect=mock_get_write_protection_error):
            # Protected path should return None (not staged, let core handle)
            result = engine.plan_redirect(
                "~/.ssh/authorized_keys",
                "write_file",
                "task",
                workspace=self.workspace,
                storage=self.storage,
            )
            self.assertIsNone(result)
            
            # Normal path should be staged
            result = engine.plan_redirect(
                "note.txt",
                "write_file",
                "task",
                workspace=self.workspace,
                storage=self.storage,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["action"], "modify")

    def test_apply_respects_hermes_protections(self):
        """stage_apply should enforce Hermes protections even for previously staged files."""
        
        # Stage a file first
        note = self.workspace / "note.txt"
        note.write_text("OLD\n", encoding="utf-8")
        
        directive = engine.plan_redirect(
            "note.txt",
            "write_file",
            "task",
            workspace=self.workspace,
            storage=self.storage,
        )
        self.assertEqual(directive["action"], "modify")
        scratch = Path(directive["args"]["path"])
        scratch.write_text("NEW\n", encoding="utf-8")
        
        # Create a mock module for agent.file_safety
        mock_file_safety = ModuleType("file_safety")
        def mock_get_write_denied_error(path: str) -> str | None:
            if "note.txt" in path:
                return "Write denied: test protection"
            return None
        mock_file_safety.get_write_denied_error = mock_get_write_denied_error
        
        mock_agent = ModuleType("agent")
        mock_agent.file_safety = mock_file_safety
        sys.modules["agent"] = mock_agent
        sys.modules["agent.file_safety"] = mock_file_safety
        
        try:
            # Apply should now fail due to protection
            result = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
            self.assertEqual(result["applied"], [])
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("Hermes write protection", result["errors"][0]["error"])
            
            # Original should be unchanged
            self.assertEqual(note.read_text(encoding="utf-8"), "OLD\n")
        finally:
            # Clean up mock modules
            sys.modules.pop("agent", None)
            sys.modules.pop("agent.file_safety", None)

    def test_workspace_from_task_context(self):
        """Workspace resolution should use task_context['cwd'] when available."""
        
        # Create a different workspace directory
        alt_workspace = self.root / "alt_workspace"
        alt_workspace.mkdir()
        alt_note = alt_workspace / "note.txt"
        alt_note.write_text("ALT\n", encoding="utf-8")
        
        # Use task_context to specify the workspace
        task_context = {"cwd": str(alt_workspace)}
        
        directive = engine.plan_redirect(
            "note.txt",
            "write_file",
            "task",
            workspace=alt_workspace,
            storage=self.storage,
            task_context=task_context,
        )
        
        self.assertEqual(directive["action"], "modify")
        scratch = Path(directive["args"]["path"])
        scratch.write_text("NEW\n", encoding="utf-8")
        
        # Apply should work in the alt workspace
        result = engine.stage_apply("task", workspace=alt_workspace, storage=self.storage, task_context=task_context)
        self.assertEqual(result["applied"], ["note.txt"])
        self.assertEqual(alt_note.read_text(encoding="utf-8"), "NEW\n")

    def test_workspace_root_falls_back_to_cwd(self):
        """workspace_root should fall back to Path.cwd() when task_context is invalid."""
        
        # Test with None
        root = engine.workspace_root(None)
        self.assertTrue(root.is_absolute())
        
        # Test with empty dict
        root = engine.workspace_root({})
        self.assertTrue(root.is_absolute())
        
        # Test with invalid cwd
        root = engine.workspace_root({"cwd": ""})
        self.assertTrue(root.is_absolute())
        
        # Test with relative cwd (should be rejected and fall back)
        root = engine.workspace_root({"cwd": "relative/path"})
        self.assertTrue(root.is_absolute())


if __name__ == "__main__":
    unittest.main()
