"""Systemd service installer and multi-user daemon manager for agy-cli-manager."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_MARKER_DIR = ".agy-tokens-and-settings"
SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")
DEFAULT_BIN_DIR = Path("/usr/local/bin")

UNIT_TEMPLATE_CONTENT = """[Unit]
Description=Antigravity CLI Account Manager – log watcher for %i
Documentation=https://github.com/klepsydra/agy-cli-manager
After=network.target

[Service]
Type=simple
User=%i
WorkingDirectory=~
# Systemd automatically sets HOME to the user's home directory.
Environment=AGY_CLI_MANAGER_BIN=

ExecStart=/usr/local/bin/agy-cli-manager-for-user watch
Restart=always
RestartSec=10
TimeoutStopSec=15

StandardOutput=journal
StandardError=journal
SyslogIdentifier=agy-cli-manager-watch-%i

[Install]
WantedBy=multi-user.target
"""

WRAPPER_SCRIPT_CONTENT = """#!/usr/bin/env bash
# agy-cli-manager-for-user
#
# Wrapper that locates the correct agy-cli-manager binary for the current user
# and exec's it with all arguments passed through.
#
# Search order:
#   1. $AGY_CLI_MANAGER_BIN (explicit override)
#   2. ~/.local/bin/agy-cli-manager
#   3. ~/.venv/bin/agy-cli-manager
#   4. ~/.venvs/agy-cli-manager-*/bin/agy-cli-manager
#   5. First hit on PATH

set -euo pipefail

USER_NAME="$(id -un)"
USER_HOME="${HOME:-$(getent passwd "$USER_NAME" | cut -d: -f6)}"
USER_HOME="${USER_HOME:-/home/$USER_NAME}"
export HOME="$USER_HOME"

find_binary() {
    if [[ -n "${AGY_CLI_MANAGER_BIN:-}" && -x "$AGY_CLI_MANAGER_BIN" ]]; then
        echo "$AGY_CLI_MANAGER_BIN"
        return 0
    fi

    local candidates=(
        "${USER_HOME}/.local/bin/agy-cli-manager"
        "${USER_HOME}/.venv/bin/agy-cli-manager"
    )
    for bin in "${candidates[@]}"; do
        if [[ -x "$bin" ]]; then
            echo "$bin"
            return 0
        fi
    done

    # Check ~/.venvs/agy-cli-manager-*/bin/agy-cli-manager (created by agy-deploy.sh)
    for bin in "${USER_HOME}"/.venvs/agy-cli-manager-*/bin/agy-cli-manager; do
        if [[ -x "$bin" ]]; then
            echo "$bin"
            return 0
        fi
    done

    # Fall back to PATH
    if command -v agy-cli-manager &>/dev/null; then
        command -v agy-cli-manager
        return 0
    fi

    echo "agy-cli-manager-for-user: cannot find agy-cli-manager binary for user '${USER_NAME}'" >&2
    echo "  Checked: ~/.local/bin, ~/.venv/bin, ~/.venvs/agy-cli-manager-*, and PATH" >&2
    echo "  Set AGY_CLI_MANAGER_BIN to override." >&2
    exit 1
}

bin=$(find_binary)
exec "$bin" "$@"
"""

ENABLE_SCRIPT_CONTENT = """#!/usr/bin/env bash
# agy-cli-manager-watch-enable
#
# Discovers users who have ~/.agy-tokens-and-settings/ and manages the
# agy-cli-manager-watch@<user>.service systemd instance for each.
#
# Usage:
#   agy-cli-manager-watch-enable [--dry-run] [--disable] [--status] [--scan] [USER...]
#
# With no USER arguments, scans all home directories under /home/ and /root.
# Requires root (sudo).

set -euo pipefail

UNIT_TEMPLATE="agy-cli-manager-watch@"
MARKER_DIR="${AGY_MARKER_DIR:-.agy-tokens-and-settings}"
DRY_RUN=false
ACTION="enable"   # enable | disable | status | scan
EXPLICIT_USERS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)   DRY_RUN=true ;;
        --disable)   ACTION="disable" ;;
        --status)    ACTION="status" ;;
        --scan)      ACTION="scan" ;;
        --marker)    shift; MARKER_DIR="$1" ;;
        --help|-h)
            cat <<'EOF'
Usage: agy-cli-manager-watch-enable [OPTIONS] [USER...]

Discovers users with ~/.agy-tokens-and-settings/ and manages the
agy-cli-manager-watch@<user>.service systemd unit for each.

Options:
  --dry-run      Show what would be done without making changes
  --disable      Disable and stop the service instead of enabling it
  --status       Show current status for all discovered users
  --scan         Sync running units with discovered users (enable new, stop removed)
  --marker DIR   Marker directory name (default: .agy-tokens-and-settings)
  -h, --help     Show this help
EOF
            exit 0
            ;;
        -*) echo "Unknown option: $1" >&2; exit 1 ;;
        *)  EXPLICIT_USERS+=("$1") ;;
    esac
    shift
done

if [[ $EUID -ne 0 ]] && [[ "$DRY_RUN" == false ]] && [[ "$ACTION" != "status" ]]; then
    echo "error: must be run as root (use sudo)" >&2
    exit 1
fi

discover_users() {
    local checked=()
    # Check all entries in /etc/passwd with valid homes
    while IFS=: read -r username _ uid _ _ homedir _; do
        [[ -z "$username" || -z "$homedir" ]] && continue
        if [[ -d "${homedir}/${MARKER_DIR}" ]]; then
            echo "$username"
        fi
    done < /etc/passwd
}

if [[ ${#EXPLICIT_USERS[@]} -gt 0 ]]; then
    USERS=("${EXPLICIT_USERS[@]}")
else
    mapfile -t USERS < <(discover_users | sort -u)
fi

user_home() {
    local user="$1"
    if [[ "$user" == "root" ]]; then echo "/root"; return; fi
    getent passwd "$user" | cut -d: -f6
}

runcmd() {
    if [[ "$DRY_RUN" == true ]]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

echo "agy-cli-manager-watch-enable: action=${ACTION} users=${USERS[*]:-(none)}"
echo ""

case "$ACTION" in
    status)
        for user in "${USERS[@]}"; do
            unit="${UNIT_TEMPLATE}${user}.service"
            echo "── $user ────────────────────────────────"
            systemctl status "$unit" --no-pager -l 2>&1 | head -15 || true
            echo ""
        done
        ;;
    enable)
        for user in "${USERS[@]}"; do
            unit="${UNIT_TEMPLATE}${user}.service"
            home=$(user_home "$user" 2>/dev/null || true)
            echo "── $user (home: ${home:-unknown}) ──────────────────"
            if ! id "$user" &>/dev/null; then
                echo "  SKIP: user '$user' does not exist"
                continue
            fi
            if [[ -n "$home" && ! -d "${home}/${MARKER_DIR}" ]]; then
                echo "  SKIP: ${home}/${MARKER_DIR} does not exist"
                continue
            fi
            echo "  Enabling and starting ${unit}..."
            runcmd systemctl daemon-reload
            runcmd systemctl enable "$unit"
            runcmd systemctl restart "$unit"
            if [[ "$DRY_RUN" == false ]]; then
                sleep 0.5
                systemctl is-active "$unit" >/dev/null && echo "  ✓ running" || echo "  ✗ failed to start"
            fi
            echo ""
        done
        ;;
    disable)
        for user in "${USERS[@]}"; do
            unit="${UNIT_TEMPLATE}${user}.service"
            echo "── Stopping and disabling ${unit} ──────────────────"
            runcmd systemctl stop "$unit" || true
            runcmd systemctl disable "$unit" || true
            echo ""
        done
        ;;
    scan)
        runcmd systemctl daemon-reload
        # Enable all discovered users with marker directory
        for user in "${USERS[@]}"; do
            unit="${UNIT_TEMPLATE}${user}.service"
            if ! systemctl is-active "$unit" &>/dev/null; then
                echo "Discovered new user with ~/${MARKER_DIR}: $user. Starting ${unit}..."
                runcmd systemctl enable "$unit"
                runcmd systemctl restart "$unit"
            fi
        done
        # Stop any active units whose user no longer has the marker directory
        for active_unit in $(systemctl list-units --type=service --state=running "${UNIT_TEMPLATE}*.service" --no-legend 2>/dev/null | awk '{print $1}'); do
            user="${active_unit#$UNIT_TEMPLATE}"
            user="${user%.service}"
            home=$(user_home "$user" 2>/dev/null || true)
            if [[ -n "$home" && ! -d "${home}/${MARKER_DIR}" ]]; then
                echo "User '$user' no longer has ~/${MARKER_DIR}. Stopping ${active_unit}..."
                runcmd systemctl stop "$active_unit" || true
                runcmd systemctl disable "$active_unit" || true
            fi
        done
        ;;
esac

echo "Done."
"""

SCAN_SERVICE_CONTENT = """[Unit]
Description=Scan for users with ~/.agy-tokens-and-settings/ and update agy watch daemons
Documentation=https://github.com/klepsydra/agy-cli-manager
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/agy-cli-manager-watch-enable --scan
StandardOutput=journal
StandardError=journal
SyslogIdentifier=agy-cli-manager-watch-scan
"""

SCAN_TIMER_CONTENT = """[Unit]
Description=Periodically scan for users with ~/.agy-tokens-and-settings/
Documentation=https://github.com/klepsydra/agy-cli-manager

[Timer]
OnBootSec=1min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
"""


def _run_cmd(cmd: list[str], check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
    """Run a command with sudo if not root and required."""
    needs_sudo = os.geteuid() != 0 and (
        cmd[0] in ("systemctl", "cp", "install", "chmod", "rm")
        or any(str(a).startswith(("/etc/", "/usr/local/")) for a in cmd)
    )
    if needs_sudo:
        cmd = ["sudo"] + cmd
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True)


def discover_users(marker_dir: str = DEFAULT_MARKER_DIR) -> list[dict[str, Any]]:
    """Scan all system users for the marker directory."""
    discovered: list[dict[str, Any]] = []
    seen_users: set[str] = set()

    # Passwd inspection
    try:
        entries = pwd.getpwall()
    except Exception:
        entries = []

    for entry in entries:
        username = entry.pw_name
        if username in seen_users:
            continue
        homedir = Path(entry.pw_dir)
        try:
            marker = homedir / marker_dir
            has_marker = marker.is_dir()
        except OSError:
            has_marker = False

        if has_marker:
            seen_users.add(username)
            binary = _find_user_binary(homedir)
            discovered.append({
                "user": username,
                "uid": entry.pw_uid,
                "home": str(homedir),
                "marker_path": str(marker),
                "binary": str(binary) if binary else None,
            })

    # Also check /home/* and /root in case not in passwd or special mounts
    home_candidates = [Path("/root")]
    try:
        home_candidates.extend(list(Path("/home").glob("*")))
    except OSError:
        pass

    for h in home_candidates:
        try:
            if not h.is_dir():
                continue
        except OSError:
            continue

        user = h.name if h.name != "root" else "root"
        if user in seen_users:
            continue

        try:
            marker = h / marker_dir
            has_marker = marker.is_dir()
        except OSError:
            has_marker = False

        if has_marker:
            seen_users.add(user)
            binary = _find_user_binary(h)
            try:
                p_uid = pwd.getpwnam(user).pw_uid
            except (KeyError, OSError):
                p_uid = -1
            discovered.append({
                "user": user,
                "uid": p_uid,
                "home": str(h),
                "marker_path": str(marker),
                "binary": str(binary) if binary else None,
            })

    return sorted(discovered, key=lambda x: x["user"])


def _find_user_binary(home: Path) -> Path | None:
    """Find the agy-cli-manager binary for a given user's home."""
    candidates = [
        home / ".local" / "bin" / "agy-cli-manager",
        home / ".venv" / "bin" / "agy-cli-manager",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c

    venvs_dir = home / ".venvs"
    if venvs_dir.is_dir():
        for d in venvs_dir.glob("agy-cli-manager-*"):
            b = d / "bin" / "agy-cli-manager"
            if b.is_file() and os.access(b, os.X_OK):
                return b

    # Check system PATH
    sys_bin = shutil.which("agy-cli-manager")
    if sys_bin:
        return Path(sys_bin)

    return None


def daemon_status(marker_dir: str = DEFAULT_MARKER_DIR, users: list[str] | None = None) -> list[dict[str, Any]]:
    """Get status of the daemon for all discovered or specified users."""
    if users:
        target_users = users
        all_info = {u["user"]: u for u in discover_users(marker_dir)}
    else:
        discovered = discover_users(marker_dir)
        target_users = [u["user"] for u in discovered]
        all_info = {u["user"]: u for u in discovered}

    results: list[dict[str, Any]] = []
    for user in target_users:
        unit = f"agy-cli-manager-watch@{user}.service"
        active = False
        enabled = False
        status_line = "unknown"

        # Check active
        res_active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        if res_active.returncode == 0 and res_active.stdout.strip() == "active":
            active = True

        # Check enabled
        res_enabled = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True)
        if res_enabled.returncode == 0 and res_enabled.stdout.strip() == "enabled":
            enabled = True

        info = all_info.get(user, {})
        results.append({
            "user": user,
            "unit": unit,
            "active": active,
            "enabled": enabled,
            "home": info.get("home"),
            "has_marker": bool(info.get("marker_path")),
            "binary": info.get("binary"),
        })

    return results


def daemon_install(
    marker_dir: str = DEFAULT_MARKER_DIR,
    enable_all: bool = True,
    install_scan_timer: bool = True,
    bin_dir: Path = DEFAULT_BIN_DIR,
    systemd_dir: Path = SYSTEMD_SYSTEM_DIR,
) -> dict[str, Any]:
    """Install the systemd unit templates, helper scripts, and optionally start instances."""
    tmp_dir = Path("/tmp/agy-daemon-install")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Write files to temp
        unit_file = tmp_dir / "agy-cli-manager-watch@.service"
        unit_file.write_text(UNIT_TEMPLATE_CONTENT)

        wrapper_file = tmp_dir / "agy-cli-manager-for-user"
        wrapper_file.write_text(WRAPPER_SCRIPT_CONTENT)

        enable_file = tmp_dir / "agy-cli-manager-watch-enable"
        enable_file.write_text(ENABLE_SCRIPT_CONTENT)

        scan_service_file = tmp_dir / "agy-cli-manager-watch-scan.service"
        scan_service_file.write_text(SCAN_SERVICE_CONTENT)

        scan_timer_file = tmp_dir / "agy-cli-manager-watch-scan.timer"
        scan_timer_file.write_text(SCAN_TIMER_CONTENT)

        # Install files into destination
        _run_cmd(["install", "-m", "0644", str(unit_file), str(systemd_dir / "agy-cli-manager-watch@.service")])
        _run_cmd(["install", "-m", "0755", str(wrapper_file), str(bin_dir / "agy-cli-manager-for-user")])
        _run_cmd(["install", "-m", "0755", str(enable_file), str(bin_dir / "agy-cli-manager-watch-enable")])
        _run_cmd(["install", "-m", "0644", str(scan_service_file), str(systemd_dir / "agy-cli-manager-watch-scan.service")])
        _run_cmd(["install", "-m", "0644", str(scan_timer_file), str(systemd_dir / "agy-cli-manager-watch-scan.timer")])

        # Reload systemd
        _run_cmd(["systemctl", "daemon-reload"])

        # Enable scan timer
        if install_scan_timer:
            _run_cmd(["systemctl", "enable", "--now", "agy-cli-manager-watch-scan.timer"])

        enabled_users: list[str] = []
        if enable_all:
            # Enable for all discovered users
            discovered = discover_users(marker_dir)
            for u in discovered:
                uname = u["user"]
                unit = f"agy-cli-manager-watch@{uname}.service"
                _run_cmd(["systemctl", "enable", unit])
                _run_cmd(["systemctl", "restart", unit])
                enabled_users.append(uname)

        return {
            "installed": True,
            "systemd_unit": str(systemd_dir / "agy-cli-manager-watch@.service"),
            "scan_timer": str(systemd_dir / "agy-cli-manager-watch-scan.timer"),
            "wrapper_bin": str(bin_dir / "agy-cli-manager-for-user"),
            "enable_bin": str(bin_dir / "agy-cli-manager-watch-enable"),
            "enabled_users": enabled_users,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def daemon_enable(users: list[str] | None = None, marker_dir: str = DEFAULT_MARKER_DIR) -> dict[str, Any]:
    """Enable and start watch daemon for specified or all discovered users."""
    if not users:
        users = [u["user"] for u in discover_users(marker_dir)]

    enabled: list[str] = []
    _run_cmd(["systemctl", "daemon-reload"])
    for u in users:
        unit = f"agy-cli-manager-watch@{u}.service"
        _run_cmd(["systemctl", "enable", unit])
        _run_cmd(["systemctl", "restart", unit])
        enabled.append(u)

    return {"action": "enable", "users": enabled}


def daemon_disable(users: list[str] | None = None, marker_dir: str = DEFAULT_MARKER_DIR) -> dict[str, Any]:
    """Stop and disable watch daemon for specified or all discovered users."""
    if not users:
        users = [u["user"] for u in discover_users(marker_dir)]

    disabled: list[str] = []
    for u in users:
        unit = f"agy-cli-manager-watch@{u}.service"
        _run_cmd(["systemctl", "stop", unit], check=False)
        _run_cmd(["systemctl", "disable", unit], check=False)
        disabled.append(u)

    return {"action": "disable", "users": disabled}


def daemon_scan(marker_dir: str = DEFAULT_MARKER_DIR) -> dict[str, Any]:
    """Sync active watch services with discovered users having marker directory."""
    _run_cmd(["systemctl", "daemon-reload"])
    discovered = discover_users(marker_dir)
    discovered_users = {u["user"] for u in discovered}

    started: list[str] = []
    for u in discovered:
        uname = u["user"]
        unit = f"agy-cli-manager-watch@{uname}.service"
        res = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        if res.returncode != 0:
            _run_cmd(["systemctl", "enable", unit])
            _run_cmd(["systemctl", "restart", unit])
            started.append(uname)

    return {
        "action": "scan",
        "discovered_users": sorted(discovered_users),
        "newly_started": started,
    }


def daemon_uninstall(
    bin_dir: Path = DEFAULT_BIN_DIR,
    systemd_dir: Path = SYSTEMD_SYSTEM_DIR,
) -> dict[str, Any]:
    """Stop all units and remove installed files."""
    # Discover and disable all active watch units
    res = subprocess.run(
        ["systemctl", "list-units", "--type=service", "agy-cli-manager-watch@*.service", "--no-legend"],
        capture_output=True,
        text=True,
    )
    for line in res.stdout.splitlines():
        parts = line.split()
        if parts:
            unit = parts[0]
            _run_cmd(["systemctl", "stop", unit], check=False)
            _run_cmd(["systemctl", "disable", unit], check=False)

    _run_cmd(["systemctl", "stop", "agy-cli-manager-watch-scan.timer"], check=False)
    _run_cmd(["systemctl", "disable", "agy-cli-manager-watch-scan.timer"], check=False)

    for f in [
        systemd_dir / "agy-cli-manager-watch@.service",
        systemd_dir / "agy-cli-manager-watch-scan.service",
        systemd_dir / "agy-cli-manager-watch-scan.timer",
        bin_dir / "agy-cli-manager-for-user",
        bin_dir / "agy-cli-manager-watch-enable",
    ]:
        if f.exists():
            _run_cmd(["rm", "-f", str(f)])

    _run_cmd(["systemctl", "daemon-reload"])
    return {"uninstalled": True}
