#!/usr/bin/env bash
# Deploy one app image on the VPS. The deploy workflow copies this release's compose file to
# compose.yaml.new in APP_DIR, then runs:  deploy.sh <image>
#
# Works for any account that can run docker and write APP_DIR and .env (no account is
# assumed). Before changing anything it checks .env and saves copies of .env and
# compose.yaml. On any failure after that it puts both back, starts the previous version
# again and exits non-zero.
set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/pubmedgemini}"
HEALTH_TRIES="${HEALTH_TRIES:-24}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"

fail() {
  echo "deploy failed: $*" >&2
  exit 1
}

# ---- checks; nothing is changed before these pass ----

[ "$#" -eq 1 ] || fail "usage: deploy.sh <image>"
new_image="$1"
case "$new_image" in
  "" | *[!A-Za-z0-9._:/@-]*) fail "the image name is empty or has unexpected characters" ;;
esac

cd "$APP_DIR" || fail "cannot enter $APP_DIR"
me="$(id -un)"
[ -f .env ] || fail ".env is missing in $APP_DIR"
if [ ! -r .env ] || [ ! -w .env ]; then
  fail ".env cannot be read and written by $me"
fi
[ -w . ] || fail "$APP_DIR cannot be written by $me"
lines="$(grep -c '^APP_IMAGE=' .env || true)"
[ "$lines" -ge 1 ] || fail ".env has no APP_IMAGE= line; add one as infra/README.md step 3f says"
[ "$lines" -eq 1 ] || fail ".env has more than one APP_IMAGE= line"
[ -f compose.yaml.new ] || fail "compose.yaml.new is missing; the workflow copies it first"

previous_image="$(sed -n 's/^APP_IMAGE=//p' .env)"

# ---- save the current state ----

cp -p .env .env.previous || fail "could not save a copy of .env"
had_compose=0
if [ -f compose.yaml ]; then
  cp -p compose.yaml compose.yaml.previous || fail "could not save a copy of compose.yaml"
  had_compose=1
fi

# ---- helpers ----

# Running app container uses image $1 and is healthy, and the gate container is running.
wait_until_up() {
  local want="$1" app_id gate_id image health gate_running i
  for ((i = 1; i <= HEALTH_TRIES; i++)); do
    app_id="$(docker compose ps -q app 2>/dev/null | head -n 1 || true)"
    gate_id="$(docker compose ps -q gate 2>/dev/null | head -n 1 || true)"
    if [ -n "$app_id" ] && [ -n "$gate_id" ]; then
      image="$(docker inspect --format '{{.Config.Image}}' "$app_id" 2>/dev/null || true)"
      health="$(docker inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$app_id" \
        2>/dev/null || true)"
      gate_running="$(docker inspect --format '{{.State.Running}}' "$gate_id" 2>/dev/null || true)"
      if [ "$image" = "$want" ] && [ "$health" = healthy ] && [ "$gate_running" = true ]; then
        return 0
      fi
    fi
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

restore_files() {
  # cat keeps .env's owner and mode.
  cat .env.previous > .env || return 1
  if [ "$had_compose" = 1 ]; then
    cp -p compose.yaml.previous compose.yaml || return 1
  else
    rm -f compose.yaml || return 1
  fi
}

roll_back() {
  echo "deploy failed: $*; rolling back" >&2
  if ! restore_files; then
    echo "could not restore the files; the copies are .env.previous and compose.yaml.previous" >&2
    exit 1
  fi
  rm -f .env.previous compose.yaml.previous
  if [ -z "$previous_image" ] || [ "$had_compose" = 0 ]; then
    echo "no previous version to go back to; containers are left as they are" >&2
    exit 1
  fi
  if docker compose up -d && wait_until_up "$previous_image"; then
    echo "rolled back: the previous version is healthy" >&2
  else
    echo "rollback did not come up healthy; check 'docker compose ps'" >&2
  fi
  exit 1
}

write_env() {
  local tmp
  tmp="$(mktemp .env.new.XXXXXX)" || return 1
  if ! sed "s|^APP_IMAGE=.*|APP_IMAGE=${new_image}|" .env > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  if ! cat "$tmp" > .env; then
    rm -f "$tmp"
    return 1
  fi
  rm -f "$tmp"
  grep -Fxq "APP_IMAGE=${new_image}" .env
}

# ---- deploy ----

write_env || roll_back "could not write APP_IMAGE to .env"
cp compose.yaml.new compose.yaml || roll_back "could not install the new compose.yaml"
docker compose pull app gate || roll_back "docker compose pull failed"
docker compose up -d || roll_back "docker compose up failed"
wait_until_up "$new_image" ||
  roll_back "app not healthy on the new image, or gate not running, after $HEALTH_TRIES checks"

rm -f .env.previous compose.yaml.previous compose.yaml.new
echo "deploy ok: app is healthy on $new_image and gate is running"
