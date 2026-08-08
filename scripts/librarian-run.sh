#!/usr/bin/env bash
# librarian-run.sh -- cron entrypoint for the Brain's librarian curation
# agent (ADR-0004: "fully autonomous"; docs/spec/contracts-v1.md, the
# librarian's inbox: fork/duplicate flags, lesson.candidate harvest).
#
# Runs headless Claude Code once, non-interactively, with its *only* tool
# hard-scoped to /root/brain-librarian.sh (itself hard-scoped to GET /v1/*,
# POST /v1/deposits, and POST /v1/flags/*/resolve -- see that script and
# scripts/librarian-prompt.md, the agent's working prompt). Everything the
# librarian can ever do to the Brain is bounded by those two files.
#
# Run on the Docker host (not inside a container): it shells out to
# `claude`, and brain-librarian.sh talks to the API over localhost:8300 --
# the same host-side placement as scripts/backup.sh. No cron is installed
# by this script itself; see docs/ops.md "Librarian" for the crontab line.
#
# Usage: scripts/librarian-run.sh   (no arguments)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPT_FILE="$SCRIPT_DIR/librarian-prompt.md"

LOG_DIR="/var/log/brain-librarian"
KEEP=30
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/librarian-${TIMESTAMP}.log"

mkdir -p -m 700 "$LOG_DIR"

run_status=0
(
  echo "[librarian] run started $(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if ! command -v claude >/dev/null 2>&1; then
    echo "[librarian] error: 'claude' CLI not found on PATH -- cron runs with a minimal PATH that may not" \
         "include it (same failure mode documented for 'docker' in scripts/backup.sh); fix by prefixing the" \
         "crontab line with an explicit PATH=... or by invoking claude via its full path."
    exit 1
  fi

  if [ ! -f "/root/.brain-librarian-token" ]; then
    echo "[librarian] error: /root/.brain-librarian-token does not exist -- the librarian machine has not" \
         "been minted yet. See docs/ops.md 'Librarian' section. Nothing was run."
    exit 1
  fi

  if [ ! -f "$PROMPT_FILE" ]; then
    echo "[librarian] error: prompt file not found: $PROMPT_FILE"
    exit 1
  fi

  echo "[librarian] invoking claude (model: sonnet, tools: Bash(/root/brain-librarian.sh:*) only)"

  # -p/--print: non-interactive, exits after one response -- no TTY needed,
  # safe under cron. --allowedTools pre-authorizes exactly one tool pattern;
  # anything the agent tries outside it is denied automatically (print mode
  # can't prompt interactively), so this is the entire enforcement boundary
  # -- no --dangerously-skip-permissions is used or needed.
  #
  # Deviation from the original brief's proposed `--max-turns 40`: the
  # installed CLI (verified via `claude --help`, checked at implementation
  # time) has no --max-turns flag in this version. --max-budget-usd is the
  # closest available runaway-loop guard (a hard dollar ceiling on this
  # run's API spend) and is used instead; see the implementation report for
  # the full flag verification.
  claude -p "$(cat "$PROMPT_FILE")" \
    --model sonnet \
    --allowedTools "Bash(/root/brain-librarian.sh:*)" \
    --max-budget-usd 5 \
    --output-format text

  echo "[librarian] run finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
) >> "$LOG_FILE" 2>&1 || run_status=$?

echo "[librarian] pruning logs to the last ${KEEP} in ${LOG_DIR}"
# shellcheck disable=SC2012
ls -1t "$LOG_DIR"/librarian-*.log 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
  echo "[librarian] removing old log: $old"
  rm -f "$old"
done

exit "$run_status"
