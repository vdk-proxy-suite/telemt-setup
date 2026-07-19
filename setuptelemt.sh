#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${ROOT_DIR}/config.yaml"
COMMAND="${1:-all}"
[[ $# -gt 0 ]] && shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo "--config requires a path" >&2; exit 2; }
      CONFIG_FILE="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: sudo ./setuptelemt.sh {all|0|1|2|3|links} [--config PATH]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

CONFIG_FILE="$(readlink -f "$CONFIG_FILE")"
export ROOT_DIR CONFIG_FILE
source "${ROOT_DIR}/lib/common.sh"

require_root
ensure_python_yaml
validate_config

case "$COMMAND" in
  all)
    trap 'setup_failed "$LINENO"' ERR
    run_step 0
    run_step 1
    run_step 2
    run_step 3
    trap - ERR
    ;;
  0|1|2|3)
    trap 'setup_failed "$LINENO"' ERR
    run_step "$COMMAND"
    trap - ERR
    ;;
  links) exec python3 "${ROOT_DIR}/tools/healthcheck.py" --scope links --config "$CONFIG_FILE" ;;
  *) echo "Unknown command: $COMMAND" >&2; exit 2 ;;
esac
