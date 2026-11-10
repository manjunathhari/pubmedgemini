#!/usr/bin/env bash
# Deploy one app image on the VPS. The deploy workflow copies this release's files into
# APP_DIR as compose.yaml.new and backup.sh.new (and this script as deploy.sh), then runs:
#   deploy.sh <image>
#
# Works for any account that can run docker and write APP_DIR and .env (no account is
# assumed). Before changing anything it checks .env and saves copies of .env and
# compose.yaml (.env.previous, mode 600, and compose.yaml.previous). On any failure after
# that, and on HUP, INT, TERM or PIPE (an SSH drop or a cancelled job), it puts both back,
# starts the previous version again, removes its temporary files and exits non-zero.
# backup.sh.new replaces backup.sh only after the new version is healthy.
#
# One deploy at a time: it holds an exclusive lock on APP_DIR/.deploy.lock from before it
# looks at any earlier deploy's files until it exits, and a second deploy started meanwhile
# exits non-zero without changing anything.
#
# If it was killed outright (no chance to clean up), .env.previous is still there at the
# next run. That copy is complete by construction (written to a temporary file and moved
# into place only after compose.yaml.previous), so the next run restores from it first and
# only then deploys. It never saves a half-switched .env as "previous".
set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/pubmedgemini}"
HEALTH_TRIES="${HEALTH_TRIES:-24}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
# The gate must be running, with the same restart count, at two looks this far apart.
GATE_STABLE_INTERVAL="${GATE_STABLE_INTERVAL:-5}"

phase=check # check -> saving -> switched -> finished
handling=0  # 1 once a rollback or an interrupt is being handled
new_image=""
previous_image=""
had_compose=0
locked=0 # 1 once this run holds the lock; before that it must not touch any deploy file

# Log to stderr. If stderr is gone (SSH dropped), send all further output to /dev/null
# instead of dying on the write.
say() {
  printf '%s\n' "$*" >&2 2>/dev/null || exec >/dev/null 2>&1
}

fail() {
  say "deploy failed: $*"
  exit 1
}

cleanup_tmp() {
  [ "$locked" = 1 ] || return 0
  rm -f .deploy-tmp.* 2>/dev/null || true
}

# A .env value as docker compose reads it: without a CR or surrounding spaces; if quoted,
# what is inside the quotes (anything after the closing quote, such as " # comment", is
# ignored); if not, without an unquoted trailing " # comment".
normalise() {
  local v="$1" q
  v="${v//$'\r'/}"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  case "$v" in
    \"* | \'*)
      q="${v:0:1}"
      v="${v:1}"
      v="${v%%"$q"*}"
      ;;
    *)
      v="${v%%[[:space:]]#*}"
      v="${v%"${v##*[![:space:]]}"}"
      ;;
  esac
  printf '%s' "$v"
}

# A temporary file in APP_DIR with the same content, mode and owner as $1.
tmp_copy_of() {
  local tmp
  tmp="$(mktemp .deploy-tmp.XXXXXX)" || return 1
  if cp -p "$1" "$tmp" &&
    [ "$(stat -c %u:%g:%a "$1")" = "$(stat -c %u:%g:%a "$tmp")" ]; then
    printf '%s' "$tmp"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# Put .env and compose.yaml back from the copies, each by an atomic move. The copies are
# removed only after both are back: .env.previous first, as it marks a switch in progress.
restore_files() {
  local tmp
  [ -f .env.previous ] || return 1
  if [ -f .env ]; then
    tmp="$(tmp_copy_of .env)" || return 1
  else
    tmp="$(mktemp .deploy-tmp.XXXXXX)" || return 1
  fi
  if ! { cat .env.previous > "$tmp" && mv -f "$tmp" .env; }; then
    rm -f "$tmp"
    return 1
  fi
  if [ -f compose.yaml.previous ]; then
    tmp="$(tmp_copy_of compose.yaml.previous)" || return 1
    mv -f "$tmp" compose.yaml || { rm -f "$tmp"; return 1; }
  else
    rm -f compose.yaml || return 1
  fi
  rm -f .env.previous && rm -f compose.yaml.previous
}

docker_inspect() {
  docker inspect --format "$1" "$2" 2>/dev/null || true
}

# The gate is running and was not restarted between two looks GATE_STABLE_INTERVAL apart.
gate_stable() {
  local id="$1" first second
  [ "$(docker_inspect '{{.State.Running}}' "$id")" = true ] || return 1
  first="$(docker_inspect '{{.RestartCount}}' "$id")"
  [ -n "$first" ] || return 1
  sleep "$GATE_STABLE_INTERVAL"
  [ "$(docker_inspect '{{.State.Running}}' "$id")" = true ] || return 1
  second="$(docker_inspect '{{.RestartCount}}' "$id")"
  [ "$first" = "$second" ]
}

# The running app container uses image $1 and is healthy, and the gate is stable.
wait_until_up() {
  local want="$1" app_id gate_id image health i
  for ((i = 1; i <= HEALTH_TRIES; i++)); do
    app_id="$(docker compose ps -q app 2>/dev/null | head -n 1 || true)"
    gate_id="$(docker compose ps -q gate 2>/dev/null | head -n 1 || true)"
    if [ -n "$app_id" ] && [ -n "$gate_id" ]; then
      image="$(docker_inspect '{{.Config.Image}}' "$app_id")"
      health="$(docker_inspect \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$app_id")"
      if [ "$image" = "$want" ] && [ "$health" = healthy ] && gate_stable "$gate_id"; then
        return 0
      fi
    fi
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# Runs once. Further signals are ignored while it puts the previous version back.
roll_back() {
  [ "$handling" = 0 ] || return 0
  handling=1
  trap '' HUP INT TERM PIPE
  set +e
  say "deploy failed: $*; rolling back"
  if ! restore_files; then
    say "could not restore the files; the next deploy restores them from .env.previous"
    cleanup_tmp
    exit 1
  fi
  rm -f compose.yaml.new backup.sh.new
  cleanup_tmp
  if [ -z "$previous_image" ] || [ "$had_compose" = 0 ]; then
    say "no previous version to go back to; containers are left as they are"
    exit 1
  fi
  if docker compose up -d && wait_until_up "$previous_image"; then
    say "rolled back: the previous version is healthy"
  else
    say "rollback did not come up healthy; check 'docker compose ps'"
  fi
  exit 1
}

on_signal() {
  local sig="$1"
  [ "$handling" = 0 ] || return 0
  [ "$phase" != finished ] || return 0 # the new version is in place; only clean-up is left
  case "$sig" in
    HUP | PIPE) exec >/dev/null 2>&1 ;; # the connection that read our output is gone
  esac
  if [ "$phase" = switched ]; then
    roll_back "interrupted by SIG$sig"
  fi
  handling=1
  trap '' HUP INT TERM PIPE
  say "deploy interrupted by SIG$sig before anything was changed"
  if [ "$phase" = saving ]; then
    rm -f .env.previous compose.yaml.previous
  fi
  cleanup_tmp
  exit 1
}

on_exit() {
  local code=$?
  if [ "$handling" = 0 ]; then
    case "$phase" in
      switched) roll_back "unexpected error (exit $code)" ;;
      saving) rm -f .env.previous compose.yaml.previous ;;
    esac
  fi
  cleanup_tmp
}

trap on_exit EXIT
trap 'on_signal HUP' HUP
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal PIPE' PIPE

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
[ -f compose.yaml.new ] || fail "compose.yaml.new is missing; the workflow copies it first"

# ---- one deploy at a time ----

# Held on fd 9 for the life of the script; the kernel drops it when the last copy of fd 9
# closes, however the script ends (even if killed outright). The file is kept: with no
# holder, its presence alone never blocks.
command -v flock >/dev/null 2>&1 || fail "flock is not installed (it is part of util-linux)"
exec 9>>.deploy.lock || fail "cannot open $APP_DIR/.deploy.lock"
flock -n 9 || fail "another deploy is running in $APP_DIR; nothing was changed, try again when it ends"
locked=1

# ---- an earlier deploy that was killed outright: go back to its saved state first ----

cleanup_tmp
if [ -f .env.previous ]; then
  say "found the copies left by an interrupted earlier deploy; restoring .env and compose.yaml from them first"
  restore_files || fail "could not restore from .env.previous; check $APP_DIR by hand"
elif [ -f compose.yaml.previous ]; then
  # Interrupted while saving: nothing had been changed, so this copy is not needed.
  rm -f compose.yaml.previous
fi

lines="$(grep -c '^APP_IMAGE=' .env || true)"
[ "$lines" -ge 1 ] || fail ".env has no APP_IMAGE= line; add one as infra/README.md step 3f says"
[ "$lines" -eq 1 ] || fail ".env has more than one APP_IMAGE= line"

previous_image="$(normalise "$(sed -n 's/^APP_IMAGE=//p' .env)")"

# ---- save the current state ----

phase=saving
if [ -f compose.yaml ]; then
  tmp="$(tmp_copy_of compose.yaml)" || fail "could not save a copy of compose.yaml"
  mv -f "$tmp" compose.yaml.previous || fail "could not save a copy of compose.yaml"
  had_compose=1
fi
# .env.previous is mode 600 whatever .env's mode is (mktemp makes it 600).
tmp="$(mktemp .deploy-tmp.XXXXXX)" || fail "could not save a copy of .env"
{ cat .env > "$tmp" && chmod 600 "$tmp" && mv -f "$tmp" .env.previous; } ||
  fail "could not save a copy of .env"
phase=switched

# ---- deploy ----

# Write the new .env next to the old one (same mode and owner), then move it into place.
write_env() {
  local tmp
  tmp="$(tmp_copy_of .env)" || return 1
  if sed "s|^APP_IMAGE=.*|APP_IMAGE=${new_image}|" .env > "$tmp" &&
    grep -Fxq "APP_IMAGE=${new_image}" "$tmp" && mv -f "$tmp" .env; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

install_compose() {
  local tmp
  tmp="$(tmp_copy_of compose.yaml.new)" || return 1
  mv -f "$tmp" compose.yaml || { rm -f "$tmp"; return 1; }
}

write_env || roll_back "could not write APP_IMAGE to .env"
install_compose || roll_back "could not install the new compose.yaml"
docker compose pull app gate || roll_back "docker compose pull failed"
docker compose up -d || roll_back "docker compose up failed"
wait_until_up "$new_image" ||
  roll_back "app not healthy on the new image, or gate not running steadily, after $HEALTH_TRIES checks"
if [ -f backup.sh.new ]; then
  { chmod 700 backup.sh.new && mv -f backup.sh.new backup.sh; } ||
    roll_back "could not install the new backup.sh"
fi

phase=finished
rm -f .env.previous
rm -f compose.yaml.previous compose.yaml.new
cleanup_tmp
printf '%s\n' "deploy ok: app is healthy on $new_image and gate is running" 2>/dev/null || true
