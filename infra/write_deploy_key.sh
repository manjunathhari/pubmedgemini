#!/usr/bin/env bash
# Writes the deploy key from the VPS_SSH_KEY environment variable to the file named
# in $1 (mode 600), then checks that it really is a loadable private key.
# The secret may be the key file as it is, or the key file as base64 (one line or wrapped).
# Prints only the public fingerprint. Never prints any part of the key.
set -euo pipefail
umask 077

target="${1:-}"
if [ -z "$target" ]; then
  echo "usage: VPS_SSH_KEY=... write_deploy_key.sh <file>" >&2
  exit 2
fi
if [ -z "${VPS_SSH_KEY:-}" ]; then
  echo "The VPS_SSH_KEY secret is empty or not set" >&2
  exit 1
fi

header='-----BEGIN OPENSSH PRIVATE KEY-----'

# Remove carriage returns and trailing spaces/tabs, drop blank lines at both ends,
# and end with exactly one newline.
tidy() {
  tr -d '\r' | sed -E 's/[[:blank:]]+$//' |
    awk 'NF { started = 1 } started { buf[++n] = $0 } END { while (n > 0 && buf[n] == "") n--; for (i = 1; i <= n; i++) print buf[i] }'
}

fail() {
  rm -f "$target"
  echo "VPS_SSH_KEY is not a readable private key (it may have been damaged when it was pasted)" >&2
  exit 1
}

rm -f "$target"
written=0
compact="$(printf '%s' "$VPS_SSH_KEY" | tr -d '[:space:]')"
if decoded="$(printf '%s' "$compact" | base64 -d 2>/dev/null)" &&
  printf '%s\n' "$decoded" | grep -qF -- "$header"; then
  printf '%s\n' "$decoded" | tidy > "$target"
  written=1
fi
if [ "$written" -eq 0 ]; then
  printf '%s\n' "$VPS_SSH_KEY" | tidy > "$target"
fi
chmod 600 "$target"

# Loads the key and derives the public half (output discarded). -P '' never asks for a passphrase.
if ! ssh-keygen -y -P '' -f "$target" > /dev/null 2>&1 < /dev/null; then
  fail
fi
ssh-keygen -lf "$target" 2> /dev/null | head -n 1
