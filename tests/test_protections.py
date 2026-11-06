"""Tests for Hermes write protection integration. Run: python3 tests/test_protections.py"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

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

    def test_plan_redirect_returns_none_for_hard_deny_paths(self):
        """plan_redirect should return None for paths Hermes would hard-deny."""
        
        def mock_check_write_protection(path: str) -> tuple[str | None, bool]:
            # Simulate Hermes denying ~/.ssh/authorized_keys (hard deny)
            if "authorized_keys" in path or ".ssh/authorized_keys" in path:
                return ("Write denied: protected system/credential file", True)
            return (None, False)
        
        with patch.object(engine, "_check_write_protection", side_effect=mock_check_write_protection):
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
    
    def test_plan_redirect_returns_none_for_approval_gated_paths(self):
        """plan_redirect should return None for approval-gated paths like ~/.ssh/config."""
        
        def mock_check_write_protection(path: str) -> tuple[str | None, bool]:
            # Simulate ~/.ssh/config requiring approval (not hard deny)
            if "config" in path and ".ssh" in path:
                return ("Write to ~/.ssh/config requires approval", False)
            return (None, False)
        
        with patch.object(engine, "_check_write_protection", side_effect=mock_check_write_protection):
            # Approval-gated path should return None (let core prompt)
            result = engine.plan_redirect(
                "~/.ssh/config",
                "write_file",
                "task",
                workspace=self.workspace,
                storage=self.storage,
            )
            self.assertIsNone(result)

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
        
        # Mock the protection check to simulate protection added after staging
        def mock_check_protection(path: str) -> tuple[str | None, bool]:
            if "note.txt" in path:
                return ("Write denied: test protection", True)
            return (None, False)
        
        with patch.object(engine, "_check_write_protection", side_effect=mock_check_protection):
            # Apply should now fail due to protection
            result = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
            self.assertEqual(result["applied"], [])
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("Hermes write protection", result["errors"][0]["error"])
            
            # Original should be unchanged
            self.assertEqual(note.read_text(encoding="utf-8"), "OLD\n")

    def test_workspace_from_session_cwd(self):
        """Workspace resolution should use session_cwd when available in Hermes."""
        
        # Create a different workspace directory
        alt_workspace = self.root / "alt_workspace"
        alt_workspace.mkdir()
        alt_note = alt_workspace / "note.txt"
        alt_note.write_text("ALT\n", encoding="utf-8")
        
        # Mock session_cwd to return alt workspace
        mock_plugin_session = ModuleType("plugin_session")
        mock_plugin_session.session_cwd = lambda: str(alt_workspace)
        
        mock_hermes_cli = ModuleType("hermes_cli")
        mock_hermes_cli.plugin_session = mock_plugin_session
        sys.modules["hermes_cli"] = mock_hermes_cli
        sys.modules["hermes_cli.plugin_session"] = mock_plugin_session
        
        try:
            # Directly test that workspace_root uses session_cwd
            root = engine.workspace_root()
            self.assertEqual(root, alt_workspace.resolve())
        finally:
            # Clean up mock modules
            sys.modules.pop("hermes_cli", None)
            sys.modules.pop("hermes_cli.plugin_session", None)

    def test_workspace_root_falls_back_to_cwd(self):
        """workspace_root should fall back to Path.cwd() when session_cwd unavailable."""
        
        # Ensure no Hermes mocks are active
        sys.modules.pop("hermes_cli", None)
        sys.modules.pop("hermes_cli.plugin_session", None)
        
        # Test falls back to cwd
        root = engine.workspace_root()
        self.assertTrue(root.is_absolute())
        self.assertEqual(root, Path.cwd().resolve())
    
    def test_binary_file_preserved_through_apply(self):
        """Binary files should be byte-exact copied, not corrupted by text encoding."""
        
        # Create a binary file
        binary_file = self.workspace / "binary.dat"
        binary_data = bytes([0, 1, 2, 255, 254, 253, 128, 129])
        binary_file.write_bytes(binary_data)
        
        directive = engine.plan_redirect(
            "binary.dat",
            "write_file",
            "task",
            workspace=self.workspace,
            storage=self.storage,
        )
        self.assertEqual(directive["action"], "modify")
        
        # Write new binary data to scratch
        scratch = Path(directive["args"]["path"])
        new_binary_data = bytes([255, 0, 128, 64, 32, 16, 8, 4, 2, 1])
        scratch.write_bytes(new_binary_data)
        
        # Apply and verify byte-exact copy
        result = engine.stage_apply("task", workspace=self.workspace, storage=self.storage)
        self.assertEqual(result["applied"], ["binary.dat"])
        self.assertEqual(binary_file.read_bytes(), new_binary_data)
    
    def test_fail_closed_on_protection_check_error(self):
        """If protection checks raise, they are caught and result in fail-closed (None returned)."""
        
        # The real _check_write_protection catches exceptions and returns error, is_hard_deny
        # So we test that exceptions within it result in proper error handling
        note = self.workspace / "note.txt"
        note.write_text("OLD\n", encoding="utf-8")
        
        # Mock to simulate exception being caught and converted to error
        def mock_check_returns_error(path: str) -> tuple[str | None, bool]:
            return ("Error checking write protections: test failure", True)
        
        with patch.object(engine, "_check_write_protection", side_effect=mock_check_returns_error):
            # Should return None (not staged) when protection check fails
            result = engine.plan_redirect(
                "note.txt",
                "write_file",
                "task",
                workspace=self.workspace,
                storage=self.storage,
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
