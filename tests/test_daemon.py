"""Tests for agy-cli-manager systemd daemon and multi-user service installer."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agy_cli_manager.daemon import (
    DEFAULT_MARKER_DIR,
    ENABLE_SCRIPT_CONTENT,
    SCAN_SERVICE_CONTENT,
    SCAN_TIMER_CONTENT,
    UNIT_TEMPLATE_CONTENT,
    WRAPPER_SCRIPT_CONTENT,
    _find_user_binary,
    daemon_disable,
    daemon_enable,
    daemon_install,
    daemon_scan,
    daemon_status,
    daemon_uninstall,
    discover_users,
)
from agy_cli_manager.cli import build_parser


class TestDaemonTemplates(unittest.TestCase):
    def test_unit_template_directives(self):
        self.assertIn("Description=Antigravity CLI Account Manager", UNIT_TEMPLATE_CONTENT)
        self.assertIn("User=%i", UNIT_TEMPLATE_CONTENT)
        self.assertIn("WorkingDirectory=~", UNIT_TEMPLATE_CONTENT)
        self.assertIn("ExecStart=/usr/local/bin/agy-cli-manager-for-user watch", UNIT_TEMPLATE_CONTENT)
        self.assertIn("Restart=always", UNIT_TEMPLATE_CONTENT)
        self.assertIn("StandardOutput=journal", UNIT_TEMPLATE_CONTENT)
        self.assertIn("WantedBy=multi-user.target", UNIT_TEMPLATE_CONTENT)

    def test_wrapper_script_directives(self):
        self.assertIn("#!/usr/bin/env bash", WRAPPER_SCRIPT_CONTENT)
        self.assertIn("agy-cli-manager-for-user", WRAPPER_SCRIPT_CONTENT)
        self.assertIn("find_binary", WRAPPER_SCRIPT_CONTENT)
        self.assertIn("USER_NAME=\"$(id -un)\"", WRAPPER_SCRIPT_CONTENT)
        self.assertIn("exec \"$bin\" \"$@\"", WRAPPER_SCRIPT_CONTENT)

    def test_enable_script_directives(self):
        self.assertIn("#!/usr/bin/env bash", ENABLE_SCRIPT_CONTENT)
        self.assertIn("agy-cli-manager-watch-enable", ENABLE_SCRIPT_CONTENT)
        self.assertIn("UNIT_TEMPLATE=\"agy-cli-manager-watch@\"", ENABLE_SCRIPT_CONTENT)
        self.assertIn("--scan", ENABLE_SCRIPT_CONTENT)
        self.assertIn("--status", ENABLE_SCRIPT_CONTENT)

    def test_scan_service_and_timer_directives(self):
        self.assertIn("agy-cli-manager-watch-scan", SCAN_SERVICE_CONTENT)
        self.assertIn("ExecStart=/usr/local/bin/agy-cli-manager-watch-enable --scan", SCAN_SERVICE_CONTENT)
        self.assertIn("OnUnitActiveSec=15min", SCAN_TIMER_CONTENT)
        self.assertIn("WantedBy=timers.target", SCAN_TIMER_CONTENT)


class TestDaemonDiscovery(unittest.TestCase):
    def test_find_user_binary_in_local_bin(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            target = bin_dir / "agy-cli-manager"
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)

            found = _find_user_binary(home)
            self.assertEqual(found, target)

    def test_find_user_binary_in_venv(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            bin_dir = home / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            target = bin_dir / "agy-cli-manager"
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)

            found = _find_user_binary(home)
            self.assertEqual(found, target)

    def test_find_user_binary_in_mise_venvs(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            bin_dir = home / ".venvs" / "agy-cli-manager-3.12.0" / "bin"
            bin_dir.mkdir(parents=True)
            target = bin_dir / "agy-cli-manager"
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)

            found = _find_user_binary(home)
            self.assertEqual(found, target)

    def test_discover_users_with_marker(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            user1_home = base / "user1"
            user1_home.mkdir()
            (user1_home / DEFAULT_MARKER_DIR).mkdir()

            user2_home = base / "user2"
            user2_home.mkdir()
            # user2 does NOT have the marker directory

            mock_entries = [
                MagicMock(pw_name="user1", pw_dir=str(user1_home), pw_uid=1001),
                MagicMock(pw_name="user2", pw_dir=str(user2_home), pw_uid=1002),
            ]

            with patch("pwd.getpwall", return_value=mock_entries), \
                 patch("pathlib.Path.glob", return_value=[]):
                discovered = discover_users()

            users = [u["user"] for u in discovered]
            self.assertIn("user1", users)
            self.assertNotIn("user2", users)


class TestDaemonCommands(unittest.TestCase):
    @patch("subprocess.run")
    def test_daemon_status(self, mock_run):
        mock_proc = MagicMock(returncode=0, stdout="active\n")
        mock_run.return_value = mock_proc

        with patch("agy_cli_manager.daemon.discover_users", return_value=[
            {"user": "alice", "home": "/home/alice", "marker_path": "/home/alice/.agy-tokens-and-settings"}
        ]):
            status = daemon_status()
            self.assertEqual(len(status), 1)
            self.assertEqual(status[0]["user"], "alice")
            self.assertEqual(status[0]["unit"], "agy-cli-manager-watch@alice.service")

    @patch("subprocess.run")
    def test_daemon_enable_and_disable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        res_enable = daemon_enable(users=["alice", "bob"])
        self.assertEqual(res_enable["action"], "enable")
        self.assertEqual(res_enable["users"], ["alice", "bob"])

        res_disable = daemon_disable(users=["alice", "bob"])
        self.assertEqual(res_disable["action"], "disable")
        self.assertEqual(res_disable["users"], ["alice", "bob"])

    @patch("subprocess.run")
    def test_daemon_scan(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="inactive\n")
        with patch("agy_cli_manager.daemon.discover_users", return_value=[
            {"user": "alice", "home": "/home/alice", "marker_path": "/home/alice/.agy-tokens-and-settings"}
        ]):
            res_scan = daemon_scan()
            self.assertEqual(res_scan["action"], "scan")
            self.assertIn("alice", res_scan["discovered_users"])
            self.assertIn("alice", res_scan["newly_started"])


class TestDaemonCLI(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_cli_daemon_install_args(self):
        args = self.parser.parse_args(["daemon", "install", "--systemd"])
        self.assertEqual(args.command, "daemon")
        self.assertEqual(args.daemon_action, "install")
        self.assertTrue(args.systemd)
        self.assertFalse(args.no_timer)

    def test_cli_service_alias(self):
        args = self.parser.parse_args(["service", "status"])
        self.assertEqual(args.command, "service")
        self.assertEqual(args.service_action, "status")

    def test_cli_daemon_scan_args(self):
        args = self.parser.parse_args(["daemon", "scan", "--json"])
        self.assertEqual(args.command, "daemon")
        self.assertEqual(args.daemon_action, "scan")
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
