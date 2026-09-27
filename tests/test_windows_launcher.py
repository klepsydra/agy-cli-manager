"""Platform-neutral regression tests for the manual Windows launcher flow."""

from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

from agy_cli_manager import manager as m


TOKEN_PATH = Path(".gemini/antigravity-cli/antigravity-oauth-token")


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-windows-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.paths = m.build_paths(self.base / "manager")
        with mock.patch.object(m, "default_live_dir", return_value=self.base / "live" / ".gemini"):
            m.ensure_layout(self.paths)

    def add_file_account(self, name: str) -> None:
        source = self.base / f"source-{name}"
        token = source / TOKEN_PATH
        token.parent.mkdir(parents=True)
        token.write_text(f"token-{name}", encoding="utf-8")
        m.add_account(self.paths, name, source)

    def test_switch_refuses_to_replace_credential_while_agy_is_running(self) -> None:
        self.add_file_account("work")
        with mock.patch.object(m, "is_agy_running", return_value=True):
            with self.assertRaisesRegex(ValueError, "Exit it before switching"):
                m.switch_account(self.paths, "work")

    def test_force_switch_allows_manual_override(self) -> None:
        self.add_file_account("work")
        with mock.patch.object(m, "is_agy_running", return_value=True):
            m.switch_account(self.paths, "work", force=True)
        self.assertEqual(m.load_state(self.paths)["active"], "work")

    def test_launch_activates_account_then_runs_agy(self) -> None:
        self.add_file_account("personal")
        with mock.patch.object(m, "switch_account") as switch, \
             mock.patch.object(m, "resolve_agy_binary", return_value="agy.exe"), \
             mock.patch.object(m.subprocess, "call", return_value=7) as call:
            result = m.launch_account(self.paths, "personal")
        self.assertEqual(result, 7)
        switch.assert_called_once_with(self.paths, "personal", force=False)
        self.assertEqual(call.call_args.args[0], ["agy.exe"])

    def test_import_current_replace_refreshes_existing_profile(self) -> None:
        self.add_file_account("work")
        replacement = self.base / "replacement"
        token = replacement / TOKEN_PATH
        token.parent.mkdir(parents=True)
        token.write_text("new-token", encoding="utf-8")
        with mock.patch.object(m, "_windows_active_credential_exists", return_value=True), \
             mock.patch.object(m, "_windows_capture_active_credential", return_value=True), \
             mock.patch.object(m, "_windows_profile_has_credential", return_value=True), \
             mock.patch.object(m, "_windows_activate_credential", return_value=True):
            m.import_current(self.paths, "work", replacement, overwrite=True)
        saved = m.account_dir(self.paths, "work") / TOKEN_PATH
        self.assertEqual(saved.read_text(encoding="utf-8"), "new-token")

    @unittest.skipUnless(os.name == "nt", "requires Windows Credential Manager")
    def test_windows_credential_round_trip(self) -> None:
        target = f"agy-cli-manager:test:{os.getpid()}"
        try:
            m._windows_write_credential(target, b"test-token", "test-user")
            self.assertEqual(m._windows_read_credential(target), (b"test-token", "test-user"))
        finally:
            m._windows_delete_credential(target)


if __name__ == "__main__":
    unittest.main()
