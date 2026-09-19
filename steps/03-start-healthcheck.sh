#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${TELEMT_LEGACY:-0} != 1 ]]; then
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  exec bash "$root/setuptelemt.sh" 3 "$@"
fi
source "${ROOT_DIR}/lib/common.sh"

UNIT="$(service_unit)"
systemctl enable --now "$UNIT"
python3 "${ROOT_DIR}/tools/healthcheck.py" --scope vm --config "$CONFIG_FILE"

SF="$(state_file)"
[[ -f "$SF" ]] || die "installation state is missing"
sed -i 's/^COMMITTED=.*/COMMITTED=1/' "$SF"
log "$UNIT is enabled and healthy"
