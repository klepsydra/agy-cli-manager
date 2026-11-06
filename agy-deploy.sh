#!/usr/bin/env bash
# Idempotent AGY account-manager bootstrap and maintenance.
# Usage: ./agy-deploy.sh user@targetHost
# Modes used by installed services: --local, --maintain, --refresh, --watch
set -uo pipefail

MANAGER_URL="https://github.com/klepsydra/agy-cli-manager.git"
MANAGER_UPSTREAM_URL="https://github.com/zcop/agy-cli-manager.git"
TOKENS_URL="https://github.com/klepsydra/agy-tokens-and-settings.git"
LIVE_DIR="${AGY_LIVE_DIR:-$HOME/.gemini}"
MANAGER_ROOT="${AGY_MANAGER_ROOT:-$HOME/.agy-cli-manager}"
MANAGER_REPO="$HOME/agy-cli-manager"
TOKENS_REPO="$HOME/agy-tokens-and-settings"
LOCAL_BIN="$HOME/.local/bin"
DEPLOY_BIN="$LOCAL_BIN/agy-deploy"
export PATH="$LOCAL_BIN:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin:$PATH"
export AGY_BINARY="${AGY_BINARY:-$LOCAL_BIN/agy}"
ERRORS=0

say() { printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
ok() { say "OK: $*"; }
warn() { say "WARN: $*" >&2; }
step() {
  local label="$1"; shift
  say "$label"
  if "$@"; then ok "$label"; else warn "$label failed (continuing)"; ERRORS=$((ERRORS + 1)); return 0; fi
}

local_host() {
  local user="$1" host="$2" short
  [[ "$user" == "$(id -un)" ]] || return 1
  case "$host" in localhost|127.0.0.1|::1|local|u) return 0 ;; esac
  short="$(hostname -s 2>/dev/null || hostname)"
  [[ "${host,,}" == "${short,,}" ]] && return 0
  [[ "${host,,}" == "${short,,}.local" ]] && return 0
  if command -v getent >/dev/null 2>&1 && command -v hostname >/dev/null 2>&1; then
    local resolved local_ips ip
    resolved="$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u || true)"
    local_ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | sort -u || true)"
    while IFS= read -r ip; do
      [[ -n "$ip" ]] || continue
      if grep -Fxq "$ip" <<<"$local_ips"; then return 0; fi
    done <<<"$resolved"
  fi
  return 1
}

bootstrap_remote_identity() {
  local remote="$1" source_token=""
  # Install gh before cloning the private repositories; never replace an
  # authenticated target identity. The token travels only on SSH stdin.
  if ! ssh -o ConnectTimeout=7 "$remote" 'command -v gh >/dev/null 2>&1 || { if command -v sudo >/dev/null 2>&1; then sudo apt-get update -qq && sudo apt-get install -y gh; else apt-get update -qq && apt-get install -y gh; fi; }' >/dev/null; then
    warn "Could not install GitHub CLI on $remote"; return 1
  fi
  if ! ssh -o ConnectTimeout=7 "$remote" 'gh auth status --hostname github.com >/dev/null 2>&1'; then
    if command -v gh >/dev/null 2>&1 && gh auth status --hostname github.com >/dev/null 2>&1; then
      source_token="$(gh auth token --hostname github.com 2>/dev/null)" || source_token=""
      if [[ -n "$source_token" ]]; then
        if printf '%s\n' "$source_token" | ssh -o ConnectTimeout=7 "$remote" \
          'if gh auth status --hostname github.com >/dev/null 2>&1; then cat >/dev/null; else gh auth login --hostname github.com --with-token && gh auth setup-git --hostname github.com; fi' >/dev/null && \
          ssh -o ConnectTimeout=7 "$remote" 'gh auth status --hostname github.com >/dev/null 2>&1'; then
          ok "Transferred source GitHub CLI identity because target had no authenticated GitHub account"
        else
          warn "Could not import source GitHub identity on $remote"
        fi
        unset source_token
      else
        warn "Source gh identity could not provide a token; private repo clone may prompt"
      fi
    else
      warn "Source has no authenticated gh identity to transfer"
    fi
  else
    ok "Target already has an authenticated GitHub identity; left it unchanged"
  fi

}
transfer_remote_glab_identity() {
  local remote="$1" status source_token
  command -v glab >/dev/null 2>&1 || return 0
  status="$(NO_COLOR=1 TERM=dumb glab auth status --hostname gitlab.com --show-token 2>&1)" || {
    warn "Source has no exportable GitLab.com identity"; return 0;
  }
  source_token="$(awk -F": " 'tolower($1) ~ /token/ {print $2; exit}' <<<"$status")"
  unset status
  [[ -n "$source_token" ]] || { warn "Source glab did not expose a GitLab.com token"; return 0; }
  if ssh -o ConnectTimeout=7 "$remote" 'export PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin; glab auth status --hostname gitlab.com >/dev/null 2>&1'; then
    unset source_token
    ok "Target already has an authenticated GitLab.com identity; left it unchanged"
    return 0
  fi
  if printf '%s\n' "$source_token" | ssh -o ConnectTimeout=7 "$remote" \
      'export PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin; if glab auth status --hostname gitlab.com >/dev/null 2>&1; then cat >/dev/null; else glab auth login --hostname gitlab.com --stdin --setup-git-credential-helper; fi' >/dev/null && \
      ssh -o ConnectTimeout=7 "$remote" 'export PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin; glab auth status --hostname gitlab.com >/dev/null 2>&1'; then
    ok "Transferred source GitLab.com CLI identity because target had no authenticated GitLab.com account"
  else
    warn "Could not import the source GitLab.com identity; target glab may need a device login"
  fi
  unset source_token
}

remote_bootstrap() {
  local target="$1" user host base remote_tmp candidate
  user="${target%@*}"; host="${target#*@}"
  if [[ "$user" == "$target" || -z "$user" || -z "$host" ]]; then
    warn "Target must be user@targetHost"; return 2
  fi
  if local_host "$user" "$host"; then
    exec bash "$0" --local
  fi

  base="${host%.brill-bicolor.ts.net}"
  local candidates=("$host")
  [[ "$base" != "$host" ]] && candidates+=("$base" "${base}.local")
  if [[ "$base" == "t14s" || "$host" == "t14s.local" ]]; then
    candidates+=("t14s.local" "t14s.brill-bicolor.ts.net" "192.168.3.66")
  fi
  # Try each distinct route. The script is copied to /tmp so sudo can still
  # prompt on the remote terminal; the supplied target's account owns the run.
  local seen=' '
  for candidate in "${candidates[@]}"; do
    [[ "$seen" == *" $candidate "* ]] && continue
    seen+="$candidate "
    say "Trying SSH route ${user}@${candidate}"
    remote_tmp="/tmp/agy-command-${user}-$$.sh"
    if scp -q -o ConnectTimeout=7 "$0" "${user}@${candidate}:${remote_tmp}" 2>/dev/null; then
      bootstrap_remote_identity "${user}@${candidate}" || warn "Could not transfer source CLI identity over ${candidate}; continuing"
      local remote_rc=0
      ssh -t -o ConnectTimeout=7 "${user}@${candidate}" "chmod 700 '$remote_tmp' && bash '$remote_tmp' --local; rc=\$?; rm -f '$remote_tmp'; exit \$rc" || remote_rc=$?
      if ((remote_rc != 255)); then
        transfer_remote_glab_identity "${user}@${candidate}"
        return "$remote_rc"
      fi
    fi
    warn "SSH route ${candidate} failed; trying the next route"
  done
  warn "Could not reach $target using the supplied hostname, local hostname, or known t14s LAN address."
  return 1
}

record_error() { warn "$*"; ERRORS=$((ERRORS + 1)); }

install_system_dependencies() {
  local packages=(ca-certificates curl git build-essential pkg-config libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev xz-utils cron gh)
  command -v dpkg-query >/dev/null 2>&1 || { record_error "dpkg-query is unavailable; install Ubuntu/Debian prerequisites manually"; return; }
  local missing=() package status
  for package in "${packages[@]}"; do
    status="$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)"
    [[ "$status" == "install ok installed" ]] || missing+=("$package")
  done
  ((${#missing[@]} == 0)) && { ok "Ubuntu system prerequisites already installed"; return; }
  if ! command -v sudo >/dev/null 2>&1; then record_error "Missing packages (${missing[*]}) and sudo is unavailable"; return; fi
  if ! sudo apt-get update || ! sudo apt-get install -y "${missing[@]}"; then
    record_error "Could not install missing Ubuntu prerequisites: ${missing[*]}"
  fi
}

ensure_repo() {
  local path="$1" url="$2" upstream_url="${3:-}" actual
  if [[ ! -e "$path" ]]; then
    step "Clone $(basename "$path")" git clone "$url" "$path"
  elif [[ ! -d "$path/.git" ]]; then
    record_error "$path exists but is not a Git checkout; leaving it untouched"; return
  fi
  [[ -d "$path/.git" ]] || return
  actual="$(git -C "$path" remote get-url origin 2>/dev/null || true)"
  if [[ "$actual" != "$url" ]]; then record_error "$path origin is '$actual', expected '$url'; refusing to retarget it"; return; fi
  if [[ -n "$upstream_url" ]]; then
    actual="$(git -C "$path" remote get-url upstream 2>/dev/null || true)"
    if [[ -z "$actual" ]]; then git -C "$path" remote add upstream "$upstream_url" || record_error "Could not add upstream remote in $path"
    elif [[ "$actual" != "$upstream_url" ]]; then record_error "$path upstream is '$actual', expected '$upstream_url'"; return; fi
  fi
}

update_repo() {
  local path="$1" is_manager="$2"
  [[ -d "$path/.git" ]] || { record_error "Cannot update absent checkout $path"; return; }
  if [[ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]]; then
    record_error "$path has local changes; left it untouched instead of pulling over them"; return
  fi
  if ! git -C "$path" checkout main || ! git -C "$path" fetch origin main; then record_error "Could not fetch origin/main in $path"; return; fi
  if ! git -C "$path" merge --ff-only origin/main; then record_error "Could not fast-forward $path from origin/main"; return; fi
  if [[ "$is_manager" == yes ]]; then
    if git -C "$path" fetch upstream main; then
      if ! git -C "$path" merge --no-edit upstream/main; then
        git -C "$path" merge --abort >/dev/null 2>&1 || true
        record_error "Upstream zcop changes conflict with the Klepsydra fork in $path; review manually"
        return
      fi
    else
      record_error "Could not fetch zcop upstream for $path"; return
    fi
  fi
  if ! git -C "$path" push origin main; then record_error "Could not push updated main for $path"; fi
}

ensure_toolchain() {
  mkdir -p "$LOCAL_BIN" "$HOME/.venvs"
  export PATH="$LOCAL_BIN:$HOME/.local/share/mise/shims:$PATH"
  if ! command -v mise >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || { record_error "curl is required to bootstrap mise"; return; }
    if ! curl -fsSL https://mise.run | sh; then record_error "mise installation failed"; return; fi
    export PATH="$LOCAL_BIN:$HOME/.local/share/mise/shims:$PATH"
  fi
  local mise_bin="$LOCAL_BIN/mise"
  [[ -x "$mise_bin" ]] || mise_bin="$(command -v mise)"
  step "Update mise" "$mise_bin" self-update -y
  mkdir -p "$HOME/.config/mise/conf.d"
  cat > "$HOME/.config/mise/conf.d/agy-tools.toml" <<'EOF'
[tools]
python = "latest"
uv = "latest"
ruby = "latest"
bun = "latest"
glab = "latest"
EOF
  # Install user-local language runtimes/package managers only; no system
  # Python/Ruby/Bun installation is changed.
  local tool
  for tool in python@latest uv@latest ruby@latest bun@latest glab@latest; do
    step "Install/update mise tool $tool" "$mise_bin" install "$tool"
  done
  step "Upgrade Python package manager pip" "$mise_bin" exec python@latest -- python -m pip install --upgrade pip
  step "Upgrade RubyGems" "$mise_bin" exec ruby@latest -- gem update --system

  local pyver venv
  pyver="$("$mise_bin" latest python@latest 2>/dev/null || true)"
  [[ -n "$pyver" ]] || { record_error "mise could not resolve Python latest"; return; }
  venv="$HOME/.venvs/agy-cli-manager-$pyver"
  if [[ ! -x "$venv/bin/python" ]]; then
    if ! "$mise_bin" exec "python@$pyver" -- python -m venv "$venv"; then record_error "Could not create $venv"; return; fi
  fi
  if [[ -d "$MANAGER_REPO/.git" ]]; then
    step "Upgrade manager package from the local checkout" "$venv/bin/python" -m pip install --upgrade --editable "$MANAGER_REPO"
  fi
  cat > "$LOCAL_BIN/agy-cli-manager" <<EOF
#!/usr/bin/env bash
exec "$venv/bin/agy-cli-manager" "\$@"
EOF
  chmod 755 "$LOCAL_BIN/agy-cli-manager"
  cat > "$DEPLOY_BIN" <<'EOF'
#!/usr/bin/env bash
exec bash "$HOME/agy-tokens-and-settings/agy-deploy.sh" "$@"
EOF
  chmod 755 "$DEPLOY_BIN"
  local shell_file
  for shell_file in "$HOME/.bashrc" "$HOME/.profile"; do
    if grep -q 'AGY-DEPLOY-MISE' "$shell_file" 2>/dev/null; then
      if ! grep -q 'AGY-DEPLOY-MISE-PATH' "$shell_file"; then
        sed -i '/# BEGIN AGY-DEPLOY-MISE/a export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH" # AGY-DEPLOY-MISE-PATH' "$shell_file"
      fi
    else
      cat >> "$shell_file" <<'EOF'

# BEGIN AGY-DEPLOY-MISE
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH" # AGY-DEPLOY-MISE-PATH
if [ -x "$HOME/.local/bin/mise" ]; then
  eval "$("$HOME/.local/bin/mise" activate bash)"
fi
# END AGY-DEPLOY-MISE
EOF
    fi
  done
}

ensure_agy_cli() {
  local binary="$AGY_BINARY"
  if [[ -x "$binary" ]] && "$binary" --version >/dev/null 2>&1; then
    ok "AGY CLI already installed at $binary"
    return
  fi
  command -v curl >/dev/null 2>&1 || { record_error "curl is required to install the AGY CLI"; return; }
  say "Install the Google Antigravity CLI (agy)"
  if ! curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir "$(dirname "$binary")"; then
    record_error "Official AGY CLI installer failed"
    return
  fi
  if [[ -x "$binary" ]] && "$binary" --version >/dev/null 2>&1; then
    ok "Installed AGY CLI at $binary"
  else
    record_error "AGY installer completed, but $binary is missing or not executable"
  fi
}

private_tokens_repo() {
  command -v gh >/dev/null 2>&1 || return 1
  gh auth status >/dev/null 2>&1 || return 1
  [[ "$(gh repo view klepsydra/agy-tokens-and-settings --json visibility --jq .visibility 2>/dev/null || true)" == PRIVATE ]]
}

sync_up_private_accounts() {
  if ! private_tokens_repo; then
    record_error "Skipped token/settings push: gh must be authenticated and klepsydra/agy-tokens-and-settings must report PRIVATE"
    return
  fi
  if [[ -x "$TOKENS_REPO/sync-up.sh" ]]; then
    step "Merge refreshed and unique local accounts into the private settings repo" bash "$TOKENS_REPO/sync-up.sh" "Automated account and settings sync"
  else
    record_error "sync-up.sh is missing from $TOKENS_REPO"
  fi
}

configure_manager() {
  local manager_bin="$LOCAL_BIN/agy-cli-manager"
  [[ -x "$manager_bin" ]] || { record_error "agy-cli-manager is not installed"; return; }
  step "Initialize manager state" "$manager_bin" init
  if [[ -x "$TOKENS_REPO/sync-down.sh" ]]; then
    step "Merge repository accounts/settings into this host" bash "$TOKENS_REPO/sync-down.sh" --non-interactive
  else
    record_error "sync-down.sh is missing from $TOKENS_REPO"
  fi
  step "Set the live Antigravity home" "$manager_bin" set-live-dir "$LIVE_DIR"
  step "Apply the manager's active account to the live profile" "$manager_bin" apply-active
  step "Enable automatic account switching" "$manager_bin" switch-mode auto
  step "Set balanced proactive quota thresholds" "$manager_bin" switch-policy --gemini-threshold 10 --other-threshold 10 --refresh-failure-threshold 2 --candidate-strategy balanced
  step "Select automatic credential backend" "$manager_bin" credential-backend auto
  step "Ensure a usable account is active" "$manager_bin" ensure-active --json
}

install_services() {
  local service_dir="$HOME/.config/systemd/user" cron_file tmp profile
  mkdir -p "$service_dir" "$LOCAL_BIN"
  cat > "$service_dir/agy-cli-manager-watch.service" <<EOF
[Unit]
Description=AGY account quota log watcher
After=default.target

[Service]
Type=simple
ExecStart=$LOCAL_BIN/agy-deploy --watch
Environment=PATH=$LOCAL_BIN:/usr/local/bin:/usr/bin:/bin
Environment=AGY_BINARY=$AGY_BINARY
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  cat > "$service_dir/agy-account-refresh.service" <<EOF
[Unit]
Description=Refresh AGY account quotas and proactively select a usable account

[Service]
Type=oneshot
ExecStart=/usr/bin/flock -n $MANAGER_ROOT/refresh.lock $LOCAL_BIN/agy-deploy --refresh
Environment=PATH=$LOCAL_BIN:/usr/local/bin:/usr/bin:/bin
Environment=AGY_BINARY=$AGY_BINARY
EOF
  cat > "$service_dir/agy-account-refresh.timer" <<'EOF'
[Unit]
Description=Refresh AGY account quotas every ten minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Persistent=true
Unit=agy-account-refresh.service

[Install]
WantedBy=timers.target
EOF
  cat > "$service_dir/agy-maintain.service" <<EOF
[Unit]
Description=Update AGY manager, tools, accounts, and settings

[Service]
Type=oneshot
ExecStart=/usr/bin/flock -n $MANAGER_ROOT/maintenance.lock $LOCAL_BIN/agy-deploy --maintain
Environment=PATH=$LOCAL_BIN:/usr/local/bin:/usr/bin:/bin
Environment=AGY_BINARY=$AGY_BINARY
EOF
  cat > "$service_dir/agy-maintain.timer" <<'EOF'
[Unit]
Description=Weekly AGY deployment and toolchain maintenance

[Timer]
OnCalendar=Sun *-*-* 04:15:00
Persistent=true
Unit=agy-maintain.service

[Install]
WantedBy=timers.target
EOF

  # Cron duplicates the refresh/maintenance timers and can start a watcher
  # after WSL/Linux boot. flock prevents duplicate maintenance runs.
  if command -v crontab >/dev/null 2>&1; then
    tmp="$(mktemp)"
    (crontab -l 2>/dev/null | sed '/# BEGIN AGY-DEPLOY/,/# END AGY-DEPLOY/d' || true) > "$tmp"
    cat >> "$tmp" <<EOF
# BEGIN AGY-DEPLOY
*/10 * * * * /usr/bin/flock -n $MANAGER_ROOT/refresh.lock $LOCAL_BIN/agy-deploy --refresh >> $HOME/.agy-cli-manager/cron-refresh.log 2>&1
15 4 * * 0 /usr/bin/flock -n $MANAGER_ROOT/maintenance.lock $LOCAL_BIN/agy-deploy --maintain >> $HOME/.agy-cli-manager/cron-maintain.log 2>&1
@reboot $LOCAL_BIN/agy-deploy --start-watch >> $HOME/.agy-cli-manager/cron-watch.log 2>&1
# END AGY-DEPLOY
EOF
    if crontab "$tmp"; then ok "Installed idempotent refresh/maintenance/watchdog cron jobs"; else record_error "Could not install crontab"; fi
    rm -f "$tmp"
  else
    record_error "crontab is unavailable; refresh timer and watcher service still configured where systemd is available"
  fi

  if systemctl --user daemon-reload >/dev/null 2>&1; then
    step "Enable manager log watcher service" systemctl --user enable --now agy-cli-manager-watch.service
    step "Enable periodic quota refresh timer" systemctl --user enable --now agy-account-refresh.timer
    step "Enable weekly maintenance timer" systemctl --user enable --now agy-maintain.timer
    # Allow services to survive logout when the host supports user lingering.
    if command -v loginctl >/dev/null 2>&1; then loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || warn "Could not enable user lingering; cron/login fallback remains installed"; fi
  else
    warn "systemd user manager unavailable; cron and login watcher fallbacks remain installed"
  fi

  cat > "$LOCAL_BIN/agy-watch-start" <<EOF
#!/usr/bin/env bash
mkdir -p "$MANAGER_ROOT"
exec 9>"$MANAGER_ROOT/watch.lock"
/usr/bin/flock -n 9 || exit 0
exec "$LOCAL_BIN/agy-cli-manager" watch
EOF
  chmod 755 "$LOCAL_BIN/agy-watch-start"
  profile="$HOME/.profile"
  if ! grep -q 'AGY-DEPLOY-WATCH' "$profile" 2>/dev/null; then
    cat >> "$profile" <<EOF

# BEGIN AGY-DEPLOY-WATCH
if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user is-system-running >/dev/null 2>&1; then
  "$LOCAL_BIN/agy-deploy" --start-watch >/dev/null 2>&1 &
fi
# END AGY-DEPLOY-WATCH
EOF
  fi
}

refresh_accounts() {
  local manager_bin="$LOCAL_BIN/agy-cli-manager"
  [[ -x "$manager_bin" ]] || { record_error "agy-cli-manager is unavailable for refresh"; return; }
  step "Poll new quota log lines and rotate on a quota error" "$manager_bin" watch --once --json
  step "Refresh the next due account quota" "$manager_bin" refresh-due --json
  step "Proactively ensure usable Gemini quota" "$manager_bin" ensure-active --family gemini --json
  step "Proactively ensure usable other-family quota" "$manager_bin" ensure-active --family other --json
  sync_up_private_accounts
}

local_deploy() {
  say "Deploying AGY manager for $(id -un) on $(hostname -s)"
  install_system_dependencies
  ensure_repo "$MANAGER_REPO" "$MANAGER_URL" "$MANAGER_UPSTREAM_URL"
  ensure_repo "$TOKENS_REPO" "$TOKENS_URL"
  update_repo "$MANAGER_REPO" yes
  update_repo "$TOKENS_REPO" no
  ensure_toolchain
  ensure_agy_cli
  configure_manager
  install_services
  refresh_accounts
  "$LOCAL_BIN/agy-cli-manager" status --json || record_error "Could not read final manager status"
  if ((ERRORS)); then warn "Deployment finished with $ERRORS recoverable issue(s); see warnings above."; return 1; fi
  ok "Deployment complete. Quota rollover is active; restart a running agy process after a watcher-triggered account switch."
}

maintain() {
  install_system_dependencies
  ensure_repo "$MANAGER_REPO" "$MANAGER_URL" "$MANAGER_UPSTREAM_URL"
  ensure_repo "$TOKENS_REPO" "$TOKENS_URL"
  update_repo "$MANAGER_REPO" yes
  update_repo "$TOKENS_REPO" no
  ensure_toolchain
  ensure_agy_cli
  configure_manager
  refresh_accounts
  if ((ERRORS)); then warn "Maintenance completed with $ERRORS recoverable issue(s)."; return 1; fi
}

case "${1:-}" in
  --local) local_deploy ;;
  --maintain) maintain ;;
  --refresh) refresh_accounts ;;
  --watch) exec "$LOCAL_BIN/agy-cli-manager" watch ;;
  --start-watch) exec "$LOCAL_BIN/agy-watch-start" ;;
  *@*) remote_bootstrap "$1" ;;
  *)
    cat >&2 <<'USAGE'
Usage:
  ./agy-deploy.sh user@targetHost   Bootstrap/update a local or SSH target
  ./agy-deploy.sh --local           Deploy on the current Linux user

Service modes are installed automatically: --watch, --refresh, --maintain.
USAGE
    exit 2
    ;;
esac
