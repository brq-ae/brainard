#!/usr/bin/env bash
# brain-wrapper.sh -- hard-scoped API wrapper for an external-agent librarian
# (ADR-0010 decision 2: "the powerful path"; see docs/librarian.md). This is
# the shipped, generic version of the pattern the author's own deployment
# uses at /root/brain-librarian.sh (itself modeled on /root/brain-hub.sh,
# docs/onboarding.md's session-facing wrapper) -- parameterized by
# environment instead of hardcoded paths, so anyone can drop it in.
#
# Why hard-scoped at all: an autonomous agent that curates the library runs
# unattended, with nobody reviewing its tool calls before they execute (see
# scripts/librarian-prompt.md). Handing a session with raw shell access a
# general-purpose `curl` allow-rule would let it reach any host with any
# method. This script is the alternative: the agent's *entire* reachable
# surface is exactly three verbs -- `get` (read-only, `/v1/*` only),
# `deposit` (checkpoint knowledge/events, body must come from a file
# strictly inside its own outbox directory), and `resolve` (close out one
# flag by id) -- the agent's own tool-permission config then grants Bash
# access to this one script's path and nothing broader, so this script's own
# internal scoping is the entire enforcement boundary for what the agent can
# do to the hub.
#
# What's actually enforced (precise, not aspirational -- verified on the
# wire at implementation time): every curl invocation below carries
# `--path-as-is --globoff`. Without them, curl itself -- AFTER the bash
# `[[ "$V1_PATH" == /v1/* ]]` check above has already passed -- collapses
# `..` segments (`/v1/../admin` silently becomes a request for `/admin`,
# and the percent-encoded `/v1/%2e%2e/admin` collapses the same way) and
# expands `{...}`/`[...]` as a glob, firing one request per expansion
# (`/v1/flags?x={a,b}` becomes two separate requests). `--path-as-is`
# disables the former, `--globoff` the latter, so the path curl actually
# sends on the wire is always byte-for-byte the string that already passed
# the `/v1/*` check -- confirmed empirically against all three
# subcommands. This does not by itself vouch for how the *server* routes an
# unusual literal path like `/v1/../admin` (that's the server's own
# routing behavior, outside this script), only that this script never
# silently rewrites the request into something the caller-visible check
# didn't see.
#
# Configuration (env, all optional -- defaults shown):
#   BRAINARD_URL         http://localhost:8300
#   BRAINARD_TOKEN_FILE  $HOME/.brainard-token      (mode 600, one line: the bearer token)
#   BRAINARD_OUTBOX      $HOME/.brainard-outbox     (mode 700; deposit files must resolve inside it)
#
# Usage:
#   brain-wrapper.sh get <v1 path, e.g. "/v1/flags?unresolved=true">
#   brain-wrapper.sh deposit <path-to-*.json-file-inside-the-outbox>
#   brain-wrapper.sh resolve <flag-id>
set -euo pipefail

BRAINARD_URL="${BRAINARD_URL:-http://localhost:8300}"
BRAINARD_TOKEN_FILE="${BRAINARD_TOKEN_FILE:-$HOME/.brainard-token}"
BRAINARD_OUTBOX="${BRAINARD_OUTBOX:-$HOME/.brainard-outbox}"

usage() {
  echo "usage: $(basename "$0") get <v1 path, e.g. /v1/flags?unresolved=true>" >&2
  echo "       $(basename "$0") deposit <path-to-*.json-file-inside-\$BRAINARD_OUTBOX>" >&2
  echo "       $(basename "$0") resolve <flag-id>" >&2
  echo "" >&2
  echo "env: BRAINARD_URL (default http://localhost:8300)," >&2
  echo "     BRAINARD_TOKEN_FILE (default \$HOME/.brainard-token)," >&2
  echo "     BRAINARD_OUTBOX (default \$HOME/.brainard-outbox)" >&2
  exit 2
}

[[ $# -eq 2 ]] || usage

if [[ ! -f "$BRAINARD_TOKEN_FILE" ]]; then
  echo "error: token file not found: $BRAINARD_TOKEN_FILE" >&2
  echo "       Mint a machine token (owner token required -- UI: Admin > Machines, or" >&2
  echo "       POST /v1/machines) and save the plaintext token to $BRAINARD_TOKEN_FILE," >&2
  echo "       chmod 600, before running this script. See docs/librarian.md." >&2
  exit 1
fi
TOKEN="$(<"$BRAINARD_TOKEN_FILE")"

# The token is fed to curl via a config block on stdin (-K -), never as a
# -H argv value -- an argv value shows up in `ps` output for the whole
# machine to see; stdin does not.
case "$1" in
  get)
    V1_PATH="$2"
    [[ "$V1_PATH" == /v1/* ]] || { echo "error: path must start with /v1/" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
      | curl -sS -K - --max-time 30 --path-as-is --globoff "${BRAINARD_URL}${V1_PATH}"
    ;;
  deposit)
    DEPOSIT_FILE="$2"
    # The injection defense: a deposit body must be a *regular* file whose
    # fully-resolved (symlink-dereferenced) path lands strictly inside
    # $BRAINARD_OUTBOX and ends in `.json`. `realpath -e` resolves every
    # symlink component, so a symlink planted inside the outbox pointing at,
    # say, the token file or any other file outside it is rejected right
    # here, before curl ever sees it -- even if the caller's own file-write
    # permission scoping were ever loosened or misconfigured.
    RESOLVED="$(realpath -e -- "$DEPOSIT_FILE" 2>/dev/null)" \
      || { echo "error: no such file: $DEPOSIT_FILE" >&2; exit 1; }
    case "$RESOLVED" in
      "$BRAINARD_OUTBOX"/*.json) ;;
      *)
        echo "error: deposit file must resolve to a *.json file strictly inside $BRAINARD_OUTBOX (resolved to: $RESOLVED)" >&2
        exit 1
        ;;
    esac
    [[ -f "$RESOLVED" ]] || { echo "error: not a regular file: $RESOLVED" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
      | curl -sS -K - --max-time 30 --path-as-is --globoff -X POST "${BRAINARD_URL}/v1/deposits" \
          -H "Content-Type: application/json" \
          --data-binary "@${RESOLVED}"
    ;;
  resolve)
    FLAG_ID="$2"
    [[ "$FLAG_ID" =~ ^[A-Za-z0-9]+$ ]] || { echo "error: flag id must be a bare id (letters/digits only, no path or query string)" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
      | curl -sS -K - --max-time 30 --path-as-is --globoff -X POST "${BRAINARD_URL}/v1/flags/${FLAG_ID}/resolve"
    ;;
  *)
    usage
    ;;
esac
