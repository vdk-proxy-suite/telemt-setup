#!/usr/bin/env bash
set -Eeuo pipefail
source "${ROOT_DIR}/lib/common.sh"

SERVICE_NAME="$(cfg install.service_name)"
UNIT="${SERVICE_NAME}.service"
USER_NAME="$(cfg install.user)"
GROUP_NAME="$(cfg install.group)"
BINARY_PATH="$(cfg install.binary_path)"
CONFIG_PATH="$(cfg install.config_path)"
WORK_DIR="$(cfg install.work_dir)"
BACKUP_ROOT="$(cfg install.backup_root)"
STATE_DIR="$(state_dir)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_ROOT}/${SERVICE_NAME}/${TIMESTAMP}"
UNIT_PATH="/etc/systemd/system/${UNIT}"

PORT="$(cfg server.port)"
API_PORT="$(cfg server.api_listen_port)"
METRICS_PORT="$(cfg server.metrics_listen_port)"

for p in "$PORT" "$API_PORT" "$METRICS_PORT"; do
  if ss -H -lnt "sport = :$p" | grep -q .; then
    if systemctl is-active --quiet "$UNIT"; then
      log "port $p is owned while configured target $UNIT is active; it will be backed up and stopped"
    else
      die "port $p is already in use by another process"
    fi
  fi
done

if [[ -f "$UNIT_PATH" ]]; then
  existing_exec="$(systemctl show "$UNIT" -p ExecStart --value 2>/dev/null || true)"
  [[ -z "$existing_exec" || "$existing_exec" == *"$BINARY_PATH"* ]] || die "$UNIT exists but ExecStart does not use configured binary $BINARY_PATH"
fi

install -d -o root -g root -m 0700 "$BACKUP_DIR" "$STATE_DIR"
PREV_ACTIVE=inactive
PREV_ENABLED=disabled
systemctl is-active --quiet "$UNIT" && PREV_ACTIVE=active || true
systemctl is-enabled --quiet "$UNIT" && PREV_ENABLED=enabled || true

HAD_BINARY=0; HAD_CONFIG=0; HAD_UNIT=0
if [[ -f "$BINARY_PATH" ]]; then cp -a -- "$BINARY_PATH" "$BACKUP_DIR/binary"; HAD_BINARY=1; fi
if [[ -f "$CONFIG_PATH" ]]; then cp -a -- "$CONFIG_PATH" "$BACKUP_DIR/config.toml"; HAD_CONFIG=1; fi
if [[ -f "$UNIT_PATH" ]]; then cp -a -- "$UNIT_PATH" "$BACKUP_DIR/unit.service"; HAD_UNIT=1; fi

cat >"${STATE_DIR}/install-state.env" <<EOF
SERVICE_NAME=$(printf '%q' "$SERVICE_NAME")
USER_NAME=$(printf '%q' "$USER_NAME")
GROUP_NAME=$(printf '%q' "$GROUP_NAME")
BINARY_PATH=$(printf '%q' "$BINARY_PATH")
CONFIG_PATH=$(printf '%q' "$CONFIG_PATH")
WORK_DIR=$(printf '%q' "$WORK_DIR")
BACKUP_ROOT=$(printf '%q' "$BACKUP_ROOT")
BACKUP_DIR=$(printf '%q' "$BACKUP_DIR")
PREV_ACTIVE=$(printf '%q' "$PREV_ACTIVE")
PREV_ENABLED=$(printf '%q' "$PREV_ENABLED")
HAD_BINARY=$HAD_BINARY
HAD_CONFIG=$HAD_CONFIG
HAD_UNIT=$HAD_UNIT
COMMITTED=0
EOF
chmod 0600 "${STATE_DIR}/install-state.env"

if [[ "$PREV_ACTIVE" == active ]]; then
  systemctl stop "$UNIT"
fi
for p in "$PORT" "$API_PORT" "$METRICS_PORT"; do
  ss -H -lnt "sport = :$p" | grep -q . && die "port $p remains occupied after stopping configured target $UNIT"
done
log "backup stored in $BACKUP_DIR"
