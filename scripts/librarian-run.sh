#!/usr/bin/env bash
# librarian-run.sh -- cron entrypoint for the Brain's librarian curation
# agent (ADR-0004: "fully autonomous"; docs/spec/contracts-v1.md, the
# librarian's inbox: fork/duplicate flags, lesson.candidate harvest).
#
# Runs headless Claude Code once, non-interactively, with exactly two tool
# grants: Bash, hard-scoped to /root/brain-librarian.sh (itself hard-scoped
# to GET /v1/*, POST /v1/deposits, and POST /v1/flags/*/resolve), and
# Edit -- which, per Claude Code's own permission model, is the rule name
# that actually governs the Write tool too (a literal `Write(path)` rule is
# accepted but silently never consulted; only `Edit(path)` rules are, and
# they cover every file-mutating tool including Write -- verified against
# `claude --help` and the installed CLI's own startup-warning text at
# implementation time) -- scoped to the outbox directory only. See that
# script and scripts/librarian-prompt.md, the agent's working prompt.
# Everything the librarian can ever do to the Brain is bounded by those
# two grants plus the two files they point at.
#
# Why Write/Edit is granted at all: headless Claude Code's Bash tool has a
# static command scanner that rejects `<(...)` process-substitution syntax
# outright, before any permission check even runs -- so a deposit body can
# no longer be built inline and handed to brain-librarian.sh via a pipe (the
# original design). The outbox is the fix: the librarian writes its deposit
# JSON to a file under OUTBOX_DIR (Write access scoped to nowhere else),
# then passes that file's path to `brain-librarian.sh deposit`, which
# itself independently verifies (via `realpath`, symlink-proof) that the
# path truly resolves inside the outbox before ever reading it -- so even
# if the CLI-level path scoping were ever misconfigured, the wrapper script
# still refuses to read/upload anything outside that one directory.
#
# Run on the Docker host (not inside a container): by default it shells out
# to `claude`, and brain-librarian.sh talks to the API over localhost:8300 --
# the same host-side placement as scripts/backup.sh. No cron is installed
# by this script itself; see docs/ops.md "Librarian" for the crontab line.
#
# Runtime-agnostic via LIBRARIAN_AGENT_CMD (ADR-0010 decision 2; see
# docs/librarian.md): unset (the default on this deployment) reproduces
# EXACTLY the `claude -p ... --allowedTools ...` invocation below, unchanged
# -- this deployment's nightly cron is unaffected by this variable's
# existence. If LIBRARIAN_AGENT_CMD *is* set (to any value, even empty
# string), this script instead runs that command -- via `bash -c`, so it may
# be a full command line with its own flags/args -- and pipes the librarian
# prompt (the contents of scripts/librarian-prompt.md plus the current UTC
# time line, exactly what the default `claude -p` invocation would otherwise
# receive as its `-p` argument) to that command's STDIN instead. Contract for
# a non-Claude runtime: read the prompt from stdin, have a hard-scoped API
# wrapper (e.g. this repo's scripts/brain-wrapper.sh, or the deployment's own
# equivalent of brain-librarian.sh) reachable on PATH or by an absolute path
# the command already knows, and exit non-zero on failure so this script's
# own error handling/notification below still works. This script itself does
# NOT enforce a tool-permission sandbox on a custom command the way the
# default `--allowedTools` grant does for `claude` -- scoping a non-Claude
# runtime's own tool access is that runtime's responsibility (its own
# permission config, container boundary, etc.), same as it would be for any
# other unattended agent invocation.
#
# Usage: scripts/librarian-run.sh   (no arguments)

set -euo pipefail

# Repo-internal paths are always derived from this script's own location,
# never hardcoded, so the script works no matter what the repo directory
# itself is named or where it's checked out.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPT_FILE="$SCRIPT_DIR/librarian-prompt.md"

# Paths outside this repo -- fixed, root-owned locations on this deployment's
# host, not derived from SCRIPT_DIR. Each is overridable via an env var of
# the same name for a different deployment; the default is this host's
# current value, unchanged.
LOG_DIR="${LOG_DIR:-/var/log/brain-librarian}"
LIBRARIAN_WRAPPER="${LIBRARIAN_WRAPPER:-/root/brain-librarian.sh}"
LIBRARIAN_TOKEN_FILE="${LIBRARIAN_TOKEN_FILE:-/root/.brain-librarian-token}"
# Must match OUTBOX_DIR in $LIBRARIAN_WRAPPER and the --allowedTools
# Edit(...) scope below -- all three have to agree on one path.
OUTBOX_DIR="${OUTBOX_DIR:-/var/lib/brain-librarian/outbox}"

# LIBRARIAN_AGENT_CMD: deliberately tested for being SET at all (not for a
# non-empty value) via `${LIBRARIAN_AGENT_CMD+x}` below -- this is what lets
# "unset" (the untouched default) and "set" (opt into a different runtime)
# be told apart unambiguously. Left unassigned here on purpose: assigning it
# a default value with `${LIBRARIAN_AGENT_CMD:-...}`, as every other env var
# above does, would make it permanently "set" from that line on, defeating
# the unset/set distinction the byte-identical-default guarantee depends on.

KEEP=30
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/librarian-${TIMESTAMP}.log"

mkdir -p -m 700 "$LOG_DIR"
mkdir -p -m 700 "$OUTBOX_DIR"

run_status=0
(
  echo "[librarian] run started $(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # Clear the outbox from the *previous* run at the start of this one --
  # not at the end, deliberately: this run's files stay in place after it
  # finishes, available for debugging, right up until the next run starts
  # and clears them in turn. Plus a belt-and-suspenders age-based prune
  # (should never actually fire under normal nightly cron, but guards
  # against the outbox growing unbounded if the cron line is ever disabled
  # for a stretch and then re-enabled, or the clear step above is ever
  # bypassed).
  echo "[librarian] clearing outbox left over from the previous run: $OUTBOX_DIR"
  rm -f "$OUTBOX_DIR"/*.json 2>/dev/null || true
  echo "[librarian] pruning any outbox file older than 7 days"
  find "$OUTBOX_DIR" -maxdepth 1 -type f -mtime +7 -delete 2>/dev/null || true

  if [ -z "${LIBRARIAN_AGENT_CMD+x}" ] && ! command -v claude >/dev/null 2>&1; then
    echo "[librarian] error: 'claude' CLI not found on PATH -- cron runs with a minimal PATH that may not" \
         "include it (same failure mode documented for 'docker' in scripts/backup.sh); fix by prefixing the" \
         "crontab line with an explicit PATH=... or by invoking claude via its full path. (This check only" \
         "applies to the default runtime -- LIBRARIAN_AGENT_CMD is unset. A custom LIBRARIAN_AGENT_CMD is" \
         "responsible for its own PATH/reachability.)"
    exit 1
  fi

  if [ ! -f "$LIBRARIAN_TOKEN_FILE" ]; then
    echo "[librarian] error: $LIBRARIAN_TOKEN_FILE does not exist -- the librarian machine has not" \
         "been minted yet. See docs/ops.md 'Librarian' section. Nothing was run."
    exit 1
  fi

  if [ ! -f "$PROMPT_FILE" ]; then
    echo "[librarian] error: prompt file not found: $PROMPT_FILE"
    exit 1
  fi

  # The librarian's sandbox has no clock of its own -- without this, it has
  # been observed inventing a stylized/wrong timestamp (e.g. "22:00:00Z")
  # for event `ts` and deposit `client_ts` values instead of using the real
  # current time. Appended as a plain trailing line on the prompt, so it
  # reads as part of the same instructions the prompt file already gives,
  # right where deposit conventions are covered. Shared by both runtimes
  # below -- the default passes this as `-p`'s argument, a custom
  # LIBRARIAN_AGENT_CMD receives the identical text on stdin.
  NOW_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  PROMPT_WITH_TIME="$(cat "$PROMPT_FILE")

Current UTC time: ${NOW_UTC} -- use this for all ts and client_ts values."

  # agent_status is captured explicitly (`|| agent_status=$?`) rather than
  # left to `set -e` to catch on its own: a failing command that is the
  # LAST command inside an if/else branch does NOT trigger bash's errexit
  # (a documented but easy-to-miss bash quirk -- confirmed empirically
  # against this exact shape at implementation time) -- silently swallowing
  # a nonzero exit from either runtime below and breaking the run_status
  # propagation the rest of this script (and the notify-me ping) depends
  # on. Explicit capture + the `exit` below, after the if/fi, restores the
  # original (pre-LIBRARIAN_AGENT_CMD) behavior for the default branch and
  # gives the custom-runtime branch the same guarantee.
  agent_status=0
  if [ -z "${LIBRARIAN_AGENT_CMD+x}" ]; then
    echo "[librarian] invoking claude (model: sonnet, tools: Bash(${LIBRARIAN_WRAPPER}:*) + Edit(//${OUTBOX_DIR#/}/**) only)"

    # -p/--print: non-interactive, exits after one response -- no TTY needed,
    # safe under cron. --allowedTools pre-authorizes exactly these two tool
    # patterns; anything the agent tries outside them is denied automatically
    # (print mode can't prompt interactively), so this is the entire
    # enforcement boundary -- no --dangerously-skip-permissions is used or
    # needed. The `Edit(...)` rule (not `Write(...)` -- see the file header
    # comment for why) is the one that actually governs Write-tool calls too,
    # and the leading `//` forces the pattern to be read as an absolute
    # filesystem path rather than one relative to some settings-file root.
    #
    # Deviation from the original brief's proposed `--max-turns 40`: the
    # installed CLI (verified via `claude --help`, checked at implementation
    # time) has no --max-turns flag in this version. --max-budget-usd is the
    # closest available runaway-loop guard (a hard dollar ceiling on this
    # run's API spend) and is used instead; see the implementation report for
    # the full flag verification.
    claude -p "$PROMPT_WITH_TIME" \
      --model sonnet \
      --allowedTools "Bash(${LIBRARIAN_WRAPPER}:*) Edit(//${OUTBOX_DIR#/}/**)" \
      --max-budget-usd 5 \
      --output-format text || agent_status=$?
  elif [ -z "$(printf '%s' "$LIBRARIAN_AGENT_CMD" | tr -d '[:space:]')" ]; then
    # Guard against a set-but-empty/whitespace-only value: `bash -c ""`
    # (or `bash -c "   "`) exits 0 immediately without running anything,
    # which -- left unguarded -- would make this look exactly like a
    # successful run (agent_status stays 0, notify-me fires "done") even
    # though no agent, and therefore no curation work, ever ran. Treated as
    # a configuration error, not a successful no-op.
    echo "[librarian] error: LIBRARIAN_AGENT_CMD is set but empty (or whitespace-only) -- refusing to run" \
         "'bash -c \"\"', which would exit 0 immediately and silently report a successful run with no" \
         "agent actually invoked. Unset LIBRARIAN_AGENT_CMD entirely to use the default claude invocation," \
         "or set it to a real command."
    agent_status=1
  else
    # Runtime-agnostic path (ADR-0010 decision 2; see the file header
    # comment and docs/librarian.md for the full contract). `bash -c`
    # lets LIBRARIAN_AGENT_CMD be a full command line ("my-agent-cli --flag
    # value"), not just a bare executable; the prompt goes to its stdin,
    # never as an argv value (unlike the default `-p` form above) -- so it
    # never appears in that process's `ps` output either.
    echo "[librarian] invoking custom LIBRARIAN_AGENT_CMD (prompt piped via stdin): ${LIBRARIAN_AGENT_CMD}"
    printf '%s' "$PROMPT_WITH_TIME" | bash -c "$LIBRARIAN_AGENT_CMD" || agent_status=$?
  fi

  if [ "$agent_status" -ne 0 ]; then
    echo "[librarian] agent invocation failed (exit $agent_status)"
    exit "$agent_status"
  fi

  echo "[librarian] run finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
) >> "$LOG_FILE" 2>&1 || run_status=$?

# Notify the owner of this assignment's outcome -- fired by the runner
# itself (deterministic, based on run_status), not by the librarian agent,
# which has no notify-me/hooks wiring of its own inside its sandboxed
# --allowedTools grant.
if [ "$run_status" -ne 0 ]; then
    notify-me error "librarian" "Nightly run failed (exit $run_status) -- see $LOG_DIR/"
else
    notify-me done "librarian" "Nightly curation run complete."
fi

echo "[librarian] pruning logs to the last ${KEEP} in ${LOG_DIR}"
# shellcheck disable=SC2012
ls -1t "$LOG_DIR"/librarian-*.log 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
  echo "[librarian] removing old log: $old"
  rm -f "$old"
done

exit "$run_status"
