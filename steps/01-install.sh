#!/usr/bin/env bash
set -Eeuo pipefail
source "${ROOT_DIR}/lib/common.sh"

VERSION="$(cfg install.version)"
ARCH="$(cfg install.architecture)"
LIBC="$(cfg install.libc)"
EXPECTED_SHA="$(cfg install.sha256)"
USER_NAME="$(cfg install.user)"
GROUP_NAME="$(cfg install.group)"
BINARY_PATH="$(cfg install.binary_path)"
CONFIG_PATH="$(cfg install.config_path)"
WORK_DIR="$(cfg install.work_dir)"

DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl openssl python3 python3-yaml tar

[[ "$(uname -m)" == "$ARCH" ]] || die "host architecture $(uname -m) does not match $ARCH"
ldd --version 2>&1 | grep -iE 'glibc|GNU libc|Ubuntu GLIBC' >/dev/null || die "GNU libc was requested but not detected"

getent group "$GROUP_NAME" >/dev/null || groupadd --system "$GROUP_NAME"
if id "$USER_NAME" >/dev/null 2>&1; then
  [[ "$(id -gn "$USER_NAME")" == "$GROUP_NAME" ]] || die "existing user $USER_NAME has a different primary group"
else
  useradd --system --gid "$GROUP_NAME" --home-dir "$WORK_DIR" --shell /usr/sbin/nologin "$USER_NAME"
fi

install -d -o root -g "$GROUP_NAME" -m 0750 "$(dirname "$CONFIG_PATH")"
install -d -o "$USER_NAME" -g "$GROUP_NAME" -m 0750 "$WORK_DIR"
install -d -o root -g root -m 0755 "$(dirname "$BINARY_PATH")"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TMP_DIR"' EXIT
ASSET="telemt-${ARCH}-linux-${LIBC}.tar.gz"
URL="https://github.com/telemt/telemt/releases/download/${VERSION}/${ASSET}"
curl --fail --location --retry 3 --connect-timeout 15 --output "$TMP_DIR/$ASSET" "$URL"
ACTUAL_SHA="$(sha256sum "$TMP_DIR/$ASSET" | awk '{print $1}')"
[[ "$ACTUAL_SHA" == "$EXPECTED_SHA" ]] || die "SHA-256 mismatch: expected $EXPECTED_SHA, got $ACTUAL_SHA"

tar -xzf "$TMP_DIR/$ASSET" -C "$TMP_DIR"
BIN_SRC="$(find "$TMP_DIR" -type f -name telemt -print -quit)"
[[ -n "$BIN_SRC" ]] || die "telemt executable not found in release archive"
"$BIN_SRC" --version | grep -Fq "$VERSION" || die "downloaded binary version does not match $VERSION"
install -o root -g root -m 0755 "$BIN_SRC" "$BINARY_PATH"
log "installed $($BINARY_PATH --version) to $BINARY_PATH"
