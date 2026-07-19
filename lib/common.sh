#!/usr/bin/env bash

log() { printf '[telemt-setup] %s\n' "$*"; }
die() { printf '[telemt-setup] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] || die "run as root (sudo)"
}

ensure_python_yaml() {
  command -v python3 >/dev/null 2>&1 || {
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-yaml
  }
  python3 -c 'import yaml' >/dev/null 2>&1 || {
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-yaml
  }
}

cfg() {
  python3 "${ROOT_DIR}/tools/config.py" get --config "$CONFIG_FILE" --key "$1"
}

validate_config() {
  [[ -f "$CONFIG_FILE" ]] || die "config not found: $CONFIG_FILE"
  python3 "${ROOT_DIR}/tools/config.py" validate --config "$CONFIG_FILE"
}

service_unit() { printf '%s.service\n' "$(cfg install.service_name)"; }
state_dir() { printf '%s/%s\n' "$(cfg install.state_root)" "$(cfg install.service_name)"; }
state_file() { printf '%s/install-state.env\n' "$(state_dir)"; }

run_step() {
  local step="$1" path
  path="$(printf '%s/steps/%02d-' "$ROOT_DIR" "$step")"
  path="$(compgen -G "${path}*.sh" | head -n1 || true)"
  [[ -n "$path" ]] || die "step $step not found"
  log "running step $step: $(basename "$path")"
  bash "$path"
}

safe_remove_tree() {
  local path="$1" allowed_prefix="$2"
  [[ -n "$path" && "$path" != "/" ]] || die "unsafe empty/root removal path"
  [[ "$path" == "$allowed_prefix"/* ]] || die "refusing to remove $path outside $allowed_prefix"
  rm -rf --one-file-system -- "$path"
}

rollback_target() {
  local sf unit
  sf="$(state_file)"
  [[ -f "$sf" ]] || { log "no rollback state recorded"; return 0; }
  # shellcheck disable=SC1090
  source "$sf"
  [[ ${COMMITTED:-0} == 0 ]] || { log "installation already committed; rollback skipped"; return 0; }
  unit="${SERVICE_NAME}.service"
  log "rolling back target $unit"
  systemctl stop "$unit" >/dev/null 2>&1 || true

  if [[ ${HAD_BINARY:-0} == 1 ]]; then rm -f -- "$BINARY_PATH"; cp -a -- "$BACKUP_DIR/binary" "$BINARY_PATH"; else rm -f -- "$BINARY_PATH"; fi
  if [[ ${HAD_CONFIG:-0} == 1 ]]; then
    mkdir -p -- "$(dirname "$CONFIG_PATH")"
    rm -f -- "$CONFIG_PATH"
    cp -a -- "$BACKUP_DIR/config.toml" "$CONFIG_PATH"
  else
    rm -f -- "$CONFIG_PATH"
  fi
  if [[ ${HAD_UNIT:-0} == 1 ]]; then rm -f -- "/etc/systemd/system/$unit"; cp -a -- "$BACKUP_DIR/unit.service" "/etc/systemd/system/$unit"; else rm -f -- "/etc/systemd/system/$unit"; fi
  systemctl daemon-reload
  if [[ ${PREV_ENABLED:-disabled} == enabled ]]; then systemctl enable "$unit" >/dev/null 2>&1 || true; else systemctl disable "$unit" >/dev/null 2>&1 || true; fi
  if [[ ${PREV_ACTIVE:-inactive} == active ]]; then systemctl start "$unit"; fi
}

setup_failed() {
  local line="$1" rc=$?
  trap - ERR
  log "setup failed near line $line (exit $rc)"
  rollback_target || true
  exit "$rc"
}
