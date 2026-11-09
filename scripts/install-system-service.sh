#!/usr/bin/env bash
# install-system-service.sh
#
# Standalone installer for agy-cli-manager systemd multi-user watch daemon.
# Installs unit template, discovery timer, and helper scripts into /usr/local/bin and /etc/systemd/system.
#
# Usage:
#   sudo ./scripts/install-system-service.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [[ $EUID -ne 0 ]]; then
    echo "error: must be run as root (use sudo)" >&2
    exit 1
fi

echo "Installing agy-cli-manager systemd watch daemon..."

install -m 0644 "$SCRIPT_DIR/agy-cli-manager-watch@.service" "$SYSTEMD_DIR/agy-cli-manager-watch@.service"
install -m 0644 "$SCRIPT_DIR/agy-cli-manager-watch-scan.service" "$SYSTEMD_DIR/agy-cli-manager-watch-scan.service"
install -m 0644 "$SCRIPT_DIR/agy-cli-manager-watch-scan.timer" "$SYSTEMD_DIR/agy-cli-manager-watch-scan.timer"

install -m 0755 "$SCRIPT_DIR/agy-cli-manager-for-user" "$BIN_DIR/agy-cli-manager-for-user"
install -m 0755 "$SCRIPT_DIR/agy-cli-manager-watch-enable" "$BIN_DIR/agy-cli-manager-watch-enable"

systemctl daemon-reload
systemctl enable --now agy-cli-manager-watch-scan.timer

echo "Discovering users with ~/.agy-tokens-and-settings/..."
"$BIN_DIR/agy-cli-manager-watch-enable"

echo "Installation complete."
