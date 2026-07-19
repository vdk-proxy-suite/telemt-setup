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
STATE_DIR="$(state_dir)"

TMP_CONFIG="$(mktemp)"
TMP_UNIT="$(mktemp)"
trap 'rm -f -- "$TMP_CONFIG" "$TMP_UNIT"' EXIT

render_args=(render --config "$CONFIG_FILE" --output "$TMP_CONFIG")
[[ -f "$CONFIG_PATH" ]] && render_args+=(--existing-toml "$CONFIG_PATH")
python3 "${ROOT_DIR}/tools/config.py" "${render_args[@]}"

cat >"$TMP_UNIT" <<EOF
[Unit]
Description=Telemt MTProto Proxy (${SERVICE_NAME})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
Group=${GROUP_NAME}
WorkingDirectory=${WORK_DIR}
ExecStart=${BINARY_PATH} ${CONFIG_PATH}
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
UMask=0027
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

install -o root -g "$GROUP_NAME" -m 0640 "$TMP_CONFIG" "$CONFIG_PATH"
install -o root -g root -m 0644 "$TMP_UNIT" "/etc/systemd/system/$UNIT"
chown "$USER_NAME:$GROUP_NAME" "$WORK_DIR"
chmod 0750 "$WORK_DIR" "$(dirname "$CONFIG_PATH")"
systemctl daemon-reload

cat >"${STATE_DIR}/manifest.env" <<EOF
SERVICE_NAME=$(printf '%q' "$SERVICE_NAME")
USER_NAME=$(printf '%q' "$USER_NAME")
GROUP_NAME=$(printf '%q' "$GROUP_NAME")
BINARY_PATH=$(printf '%q' "$BINARY_PATH")
CONFIG_PATH=$(printf '%q' "$CONFIG_PATH")
WORK_DIR=$(printf '%q' "$WORK_DIR")
BACKUP_ROOT=$(printf '%q' "$(cfg install.backup_root)")
STATE_DIR=$(printf '%q' "$STATE_DIR")
PORT=$(printf '%q' "$(cfg server.port)")
MANAGE_UFW=$(printf '%q' "$(cfg install.manage_ufw)")
UFW_RULE_ADDED=0
EOF
chmod 0600 "${STATE_DIR}/manifest.env"

if [[ "$(cfg install.manage_ufw)" == true ]]; then
  command -v ufw >/dev/null 2>&1 || die "manage_ufw=true but ufw is not installed"
  if ! ufw status | grep -Fq "telemt-setup:${SERVICE_NAME}"; then
    ufw allow "$(cfg server.port)/tcp" comment "telemt-setup:${SERVICE_NAME}"
    sed -i 's/^UFW_RULE_ADDED=.*/UFW_RULE_ADDED=1/' "${STATE_DIR}/manifest.env"
  fi
fi
log "configuration installed without printing proxy secrets"
