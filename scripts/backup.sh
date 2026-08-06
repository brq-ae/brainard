#!/usr/bin/env bash
# Backup the Brain: pg_dump (custom format) from the `db` compose service to
# a local backups/ directory, prune to the last 14, then -- if a remote
# backup target is configured -- rsync the dump plus a git bundle of the
# repo to it over SSH.
#
# Placeholder target (docs/vision.md "Open build-time inputs: backup target
# machine"; ADR-0003 §11: "nightly pg_dump + git mirror to a second machine
# on the LAN"): no second machine exists yet. BACKUP_TARGET_HOST,
# BACKUP_TARGET_USER, BACKUP_TARGET_PATH (env or .env; see .env.example) are
# all unset until it does. Unset -> local-only mode; this script says so
# loudly rather than failing, so it is safe to wire into cron today and it
# will start pushing off-box the moment those three vars are filled in.
#
# Run from the Docker host (not inside a container): it drives the stack
# via `docker compose exec`. No cron is installed inside the container --
# see docs/ops.md for the host-side crontab line.
#
# Usage: scripts/backup.sh   (no arguments; reads .env in the repo root)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Load .env the same way docker compose itself does, so this script and the
# stack it's backing up always agree on credentials/target config.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

POSTGRES_USER="${POSTGRES_USER:-brain}"
POSTGRES_DB="${POSTGRES_DB:-brain}"

BACKUP_DIR="$REPO_ROOT/backups"
KEEP=14
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_FILE="$BACKUP_DIR/brain-${TIMESTAMP}.dump"
BUNDLE_FILE="$BACKUP_DIR/brain-repo-${TIMESTAMP}.bundle"

mkdir -p "$BACKUP_DIR"

echo "[backup] dumping database '${POSTGRES_DB}' (custom format) -> ${DUMP_FILE}"
# -T disables docker compose's pseudo-tty allocation, which is required for
# binary-safe output: pg_dump's custom format is not text, and a tty would
# mangle it on the way out through stdout redirection.
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$DUMP_FILE"
echo "[backup] dump complete: $(du -h "$DUMP_FILE" | cut -f1)"

echo "[backup] pruning local dumps to the last ${KEEP}"
# shellcheck disable=SC2012
ls -1t "$BACKUP_DIR"/brain-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
  echo "[backup] removing old dump: $old"
  rm -f "$old"
done

# Non-interactive-safe SSH options for both the standalone `ssh` call and
# rsync's `-e` transport: BatchMode=yes refuses to ever prompt for a
# password/passphrase (fails fast instead of hanging a cron job waiting on
# a TTY that doesn't exist), StrictHostKeyChecking=accept-new auto-accepts
# an unseen host key on first contact but still rejects a *changed* one
# (the normal MITM/host-reimage protection stays intact -- only the
# first-contact prompt, which cron can't answer, is skipped).
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)

# Track which of the three target vars are actually set, distinctly from
# whether *all* of them are -- lets a partial configuration (a likely typo
# or half-finished setup) be called out explicitly instead of silently
# falling back to the same quiet message used for "no backup machine yet".
missing_target_vars=()
[ -z "${BACKUP_TARGET_HOST:-}" ] && missing_target_vars+=("BACKUP_TARGET_HOST")
[ -z "${BACKUP_TARGET_USER:-}" ] && missing_target_vars+=("BACKUP_TARGET_USER")
[ -z "${BACKUP_TARGET_PATH:-}" ] && missing_target_vars+=("BACKUP_TARGET_PATH")

if [ ${#missing_target_vars[@]} -eq 0 ]; then
  echo "[backup] git bundle of the repo -> ${BUNDLE_FILE}"
  git -C "$REPO_ROOT" bundle create "$BUNDLE_FILE" --all

  echo "[backup] pushing dump + bundle to ${BACKUP_TARGET_USER}@${BACKUP_TARGET_HOST}:${BACKUP_TARGET_PATH}"
  ssh "${SSH_OPTS[@]}" "${BACKUP_TARGET_USER}@${BACKUP_TARGET_HOST}" "mkdir -p '${BACKUP_TARGET_PATH}'"
  rsync -az -e "ssh ${SSH_OPTS[*]}" "$DUMP_FILE" "$BUNDLE_FILE" "${BACKUP_TARGET_USER}@${BACKUP_TARGET_HOST}:${BACKUP_TARGET_PATH}/"
  echo "[backup] remote push complete"

  echo "[backup] pruning local bundles to the last ${KEEP}"
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/brain-repo-*.bundle 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
    echo "[backup] removing old bundle: $old"
    rm -f "$old"
  done
elif [ ${#missing_target_vars[@]} -eq 3 ]; then
  echo "[backup] BACKUP_TARGET_HOST/BACKUP_TARGET_USER/BACKUP_TARGET_PATH not all set -- remote push skipped" \
       "(placeholder mode: no backup machine exists yet, see docs/ops.md). Dump stayed local-only."
else
  echo "[backup] WARNING: partial backup target config -- ${missing_target_vars[*]} unset while the rest" \
       "are set. This looks like a misconfiguration (a typo, or a half-finished .env edit), not deliberate" \
       "placeholder mode -- placeholder mode is all three unset. Falling back to local-only for this run;" \
       "set all three (see .env.example) or unset all three to silence this warning."
fi

echo "[backup] done."
