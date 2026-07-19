#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${ROOT_DIR}/config.yaml"
YES=false
KEEP_CONFIG=false
KEEP_DATA=false
KEEP_BACKUPS=false
PURGE_USER=false
PURGE_SETUP=false
PURGE_UFW=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --yes) YES=true; shift ;;
    --keep-config) KEEP_CONFIG=true; shift ;;
    --keep-data) KEEP_DATA=true; shift ;;
    --keep-backups) KEEP_BACKUPS=true; shift ;;
    --purge-user) PURGE_USER=true; shift ;;
    --purge-setup) PURGE_SETUP=true; shift ;;
    --purge-ufw) PURGE_UFW=true; shift ;;
    -h|--help)
      echo "Usage: sudo ./cleantelemt.sh [--config PATH] [--yes] [--keep-config] [--keep-data] [--keep-backups] [--purge-user] [--purge-setup] [--purge-ufw]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

CONFIG_FILE="$(readlink -f "$CONFIG_FILE")"
export ROOT_DIR CONFIG_FILE
source "${ROOT_DIR}/lib/common.sh"
ensure_python_yaml
validate_config

SERVICE_NAME="$(cfg install.service_name)"
UNIT="${SERVICE_NAME}.service"
USER_NAME="$(cfg install.user)"
GROUP_NAME="$(cfg install.group)"
BINARY_PATH="$(cfg install.binary_path)"
CONFIG_PATH="$(cfg install.config_path)"
WORK_DIR="$(cfg install.work_dir)"
BACKUP_ROOT="$(cfg install.backup_root)"
STATE_DIR="$(state_dir)"
MANIFEST="${STATE_DIR}/manifest.env"

if [[ -f "$MANIFEST" ]]; then
  # shellcheck disable=SC1090
  source "$MANIFEST"
fi

cat <<EOF
Telemt cleanup plan for ${UNIT}:
  stop/disable: ${UNIT}
  remove unit:  /etc/systemd/system/${UNIT}
  remove binary: ${BINARY_PATH}
  remove config: ${CONFIG_PATH} (keep=${KEEP_CONFIG})
  remove data:   ${WORK_DIR} (keep=${KEEP_DATA})
  remove backups:${BACKUP_ROOT}/${SERVICE_NAME} (keep=${KEEP_BACKUPS})
  remove user:   ${USER_NAME} (purge=${PURGE_USER})
  purge UFW rule: ${PURGE_UFW}
EOF

if [[ "$YES" != true ]]; then
  echo "Dry-run only. Re-run as root with --yes to apply."
  exit 0
fi
require_root

systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
rm -f -- "/etc/systemd/system/${UNIT}"
systemctl daemon-reload
rm -f -- "$BINARY_PATH"
rmdir --ignore-fail-on-non-empty -- "$(dirname "$BINARY_PATH")" 2>/dev/null || true

if [[ "$KEEP_CONFIG" != true ]]; then
  rm -f -- "$CONFIG_PATH"
  rmdir --ignore-fail-on-non-empty -- "$(dirname "$CONFIG_PATH")" 2>/dev/null || true
fi
if [[ "$KEEP_DATA" != true && -e "$WORK_DIR" ]]; then safe_remove_tree "$WORK_DIR" "/opt"; fi
if [[ "$KEEP_BACKUPS" != true && -e "${BACKUP_ROOT}/${SERVICE_NAME}" ]]; then safe_remove_tree "${BACKUP_ROOT}/${SERVICE_NAME}" "$BACKUP_ROOT"; fi

if [[ "$PURGE_UFW" == true && ${UFW_RULE_ADDED:-0} == 1 ]] && command -v ufw >/dev/null 2>&1; then
  RULE_NUMBER="$(ufw status numbered | grep -F "telemt-setup:${SERVICE_NAME}" | head -n1 | sed -E 's/^\[[[:space:]]*([0-9]+)\].*/\1/' || true)"
  [[ "$RULE_NUMBER" =~ ^[0-9]+$ ]] && ufw --force delete "$RULE_NUMBER" || true
fi
rm -rf --one-file-system -- "$STATE_DIR"
if [[ "$PURGE_USER" == true ]]; then
  userdel "$USER_NAME" >/dev/null 2>&1 || true
  groupdel "$GROUP_NAME" >/dev/null 2>&1 || true
fi
echo "Cleanup complete for $UNIT"

if [[ "$PURGE_SETUP" == true ]]; then
  cd /
  safe_remove_tree "$ROOT_DIR" "$(dirname "$ROOT_DIR")"
fi
