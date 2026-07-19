#!/usr/bin/env bash
set -Eeuo pipefail
source "${ROOT_DIR}/lib/common.sh"

UNIT="$(service_unit)"
systemctl enable --now "$UNIT"
python3 "${ROOT_DIR}/tools/healthcheck.py" --scope vm --config "$CONFIG_FILE"

SF="$(state_file)"
[[ -f "$SF" ]] || die "installation state is missing"
sed -i 's/^COMMITTED=.*/COMMITTED=1/' "$SF"
log "$UNIT is enabled and healthy"
