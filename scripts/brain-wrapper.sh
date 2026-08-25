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
# surface is exactly five verbs -- `get` (read-only, `/v1/*` only),
# `deposit` (checkpoint knowledge/events, body must come from a file
# strictly inside its own outbox directory), `resolve` (close out one flag
# by id), `fetch` (ADR-0012 decision 12: download one room attachment into
# that room's own scoped directory, nothing else), and `cleanup` (ADR-0012
# decision 12: empty that room's own directory, no path argument accepted)
# -- the agent's own tool-permission config then grants Bash access to this
# one script's path and nothing broader, so this script's own internal
# scoping is the entire enforcement boundary for what the agent can do to
# the hub.
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
# `fetch`/`cleanup` (ADR-0012 decision 12) add a second, independent
# boundary on top of the same discipline: every path they ever touch on
# disk is *constructed*, never accepted as an argument. The caller supplies
# a bare room id (validated against a strict `^[A-Za-z0-9]{1,64}$` pattern
# before it forms any path component -- no '.', no '/', no whitespace, no
# control character can survive that check) and, for `fetch`, an optional
# display filename that is re-sanitized by this script's own allowlist
# logic (mirroring `app/room_export.py`'s `safe_filename_component`) --
# never taken from the server's `Content-Disposition` response header
# (never even read) and never taken from anything the downloaded bytes
# themselves claim to be. Because the room id can never contain a path
# separator and the filename can never contain one either, the resulting
# path is always exactly `$BRAINARD_ATTACHMENTS_DIR/<room-id>/<filename>`
# with no other shape reachable -- traversal is not merely checked for, it
# has no grammar that could produce it. `cleanup` goes one step further:
# the agent names a room id and *nothing else* -- there is no argument
# through which it could ever supply a path to delete, so it cannot name
# the wrong one. This follows the same design `deposit`'s outbox scoping
# below already uses (the caller supplies a filename, the script resolves
# and validates it stays strictly inside a fixed directory via
# `realpath -e`, never accepting a caller-chosen absolute deletion target)
# -- handing an LLM `rm` with a self-constructed argument is explicitly
# rejected, here as there.
#
# Configuration (env, all optional -- defaults shown):
#   BRAINARD_URL              http://localhost:8300
#   BRAINARD_TOKEN_FILE       $HOME/.brainard-token       (mode 600, one line: the bearer token)
#   BRAINARD_OUTBOX           $HOME/.brainard-outbox      (mode 700; deposit files must resolve inside it)
#   BRAINARD_ATTACHMENTS_DIR  $HOME/.brainard-attachments (mode 700; fetch/cleanup are scoped under here, one subdir per room id)
#   BRAINARD_FETCH_MAX_BYTES  20971520 (20 MiB)           (hard cap enforced during download, independent of any Content-Length the server reports)
#   BRAINARD_FETCH_TIMEOUT_SECS 120                       (fetch's own --max-time; larger than get/deposit/resolve's fixed 30s since attachments can be up to the server's per-file cap)
#
# Usage:
#   brain-wrapper.sh get <v1 path, e.g. "/v1/flags?unresolved=true">
#   brain-wrapper.sh deposit <path-to-*.json-file-inside-the-outbox>
#   brain-wrapper.sh resolve <flag-id>
#   brain-wrapper.sh fetch <room-id> <attachment-id> [display-filename]
#   brain-wrapper.sh cleanup <room-id>
set -euo pipefail

BRAINARD_URL="${BRAINARD_URL:-http://localhost:8300}"
BRAINARD_TOKEN_FILE="${BRAINARD_TOKEN_FILE:-$HOME/.brainard-token}"
BRAINARD_OUTBOX="${BRAINARD_OUTBOX:-$HOME/.brainard-outbox}"
BRAINARD_ATTACHMENTS_DIR="${BRAINARD_ATTACHMENTS_DIR:-$HOME/.brainard-attachments}"
BRAINARD_FETCH_MAX_BYTES="${BRAINARD_FETCH_MAX_BYTES:-20971520}"
BRAINARD_FETCH_TIMEOUT_SECS="${BRAINARD_FETCH_TIMEOUT_SECS:-120}"

usage() {
  echo "usage: $(basename "$0") get <v1 path, e.g. /v1/flags?unresolved=true>" >&2
  echo "       $(basename "$0") deposit <path-to-*.json-file-inside-\$BRAINARD_OUTBOX>" >&2
  echo "       $(basename "$0") resolve <flag-id>" >&2
  echo "       $(basename "$0") fetch <room-id> <attachment-id> [display-filename]" >&2
  echo "       $(basename "$0") cleanup <room-id>" >&2
  echo "" >&2
  echo "env: BRAINARD_URL (default http://localhost:8300)," >&2
  echo "     BRAINARD_TOKEN_FILE (default \$HOME/.brainard-token)," >&2
  echo "     BRAINARD_OUTBOX (default \$HOME/.brainard-outbox)," >&2
  echo "     BRAINARD_ATTACHMENTS_DIR (default \$HOME/.brainard-attachments)," >&2
  echo "     BRAINARD_FETCH_MAX_BYTES (default 20971520)," >&2
  echo "     BRAINARD_FETCH_TIMEOUT_SECS (default 120)" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage

# Bare-id validator shared by `resolve` (flag id), `fetch` (room id,
# attachment id) and `cleanup` (room id). Deliberately the tightest useful
# allowlist -- letters and digits only, 1-64 of them -- so a '.', '/',
# whitespace, or any control/shell-metacharacter can never survive to form
# part of a filesystem path or a URL path segment. Real ids in this system
# (server ULIDs, flag ids) are always a subset of this; the point is not to
# accept everything real ids look like, it's to make everything this
# accepts safe regardless of what it's used for next.
_valid_bare_id() {
  [[ "$1" =~ ^[A-Za-z0-9]{1,64}$ ]]
}

# Reduces an arbitrary, caller-supplied display filename to a short,
# plain-ASCII, single-path-component string safe to use as a disk filename
# -- mirrors app/room_export.py's safe_filename_component (same allowlist,
# same dot/space-trim-at-both-ends behavior, same 80-char cap, same
# fallback-on-empty posture) so the two layers agree on what "safe" means,
# without importing Python into a bash script. This is the ONLY input that
# ever contributes to the fetched file's name -- never the server's
# Content-Disposition header (this script never reads it) and never
# anything the downloaded bytes themselves claim to be.
_sanitize_filename() {
  local raw="$1" cleaned
  # Anything outside [A-Za-z0-9 _.-] (control chars, '/', newlines, shell
  # metacharacters, all non-ASCII) becomes a space -- never survives, never
  # silently dropped in a way that could splice two segments together.
  cleaned="$(printf '%s' "$raw" | tr -c 'A-Za-z0-9 _.-' ' ')"
  cleaned="$(printf '%s' "$cleaned" | tr -s ' ')"
  # Trim leading/trailing runs of '.' and ' ' from both ends -- this is what
  # turns a bare ".." (or "..." etc.) into the empty string, same as
  # Python's str.strip(". ") does for safe_filename_component.
  cleaned="$(printf '%s' "$cleaned" | sed -E 's/^[. ]+//; s/[. ]+$//')"
  cleaned="${cleaned:0:80}"
  cleaned="$(printf '%s' "$cleaned" | sed -E 's/^[. ]+//; s/[. ]+$//')"
  if [[ -z "$cleaned" ]]; then
    cleaned="attachment"
  fi
  # v1 is PDF-only (ADR-0012 decision 1/16); force the extension
  # regardless of what survived sanitizing, so the file on disk is always
  # unambiguously named as what the server contract guarantees it to be.
  case "$cleaned" in
    *.[Pp][Dd][Ff]) ;;
    *) cleaned="${cleaned}.pdf" ;;
  esac
  printf '%s' "$cleaned"
}

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
    [[ $# -eq 2 ]] || usage
    V1_PATH="$2"
    [[ "$V1_PATH" == /v1/* ]] || { echo "error: path must start with /v1/" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
      | curl -sS -K - --max-time 30 --path-as-is --globoff "${BRAINARD_URL}${V1_PATH}"
    ;;
  deposit)
    [[ $# -eq 2 ]] || usage
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
    [[ $# -eq 2 ]] || usage
    FLAG_ID="$2"
    [[ "$FLAG_ID" =~ ^[A-Za-z0-9]+$ ]] || { echo "error: flag id must be a bare id (letters/digits only, no path or query string)" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
      | curl -sS -K - --max-time 30 --path-as-is --globoff -X POST "${BRAINARD_URL}/v1/flags/${FLAG_ID}/resolve"
    ;;
  fetch)
    # ADR-0012 decision 12: scoped fetch. Writes ONLY into
    # $BRAINARD_ATTACHMENTS_DIR/<room-id>/ -- never anywhere else, by
    # construction (see the header comment). Guards, in order:
    #   1. room id and attachment id are both validated bare ids before
    #      either forms any part of a path or URL.
    #   2. the per-room directory is created (if needed) and then verified
    #      with `realpath -e` to resolve strictly inside the attachments
    #      base dir -- catches a symlink swapped in for the room's slot.
    #   3. the download goes to a *temp* file inside that same directory,
    #      never directly to the final name -- a killed/failed download
    #      never leaves a partially-written file at the name a caller might
    #      later trust.
    #   4. no redirect is ever followed (`--max-redirs 0`) and the response
    #      status is checked to be exactly 200 -- a 3xx to any host (same or
    #      different) is refused, not silently followed.
    #   5. the byte cap is enforced by piping the body through `head -c
    #      <cap+1>`, which bounds bytes actually written to disk regardless
    #      of whether the server sends a (correct, absent, or lying)
    #      Content-Length -- `--max-filesize` is layered on top as a
    #      cheaper up-front check when Content-Length is present, but is not
    #      relied on alone (curl's own documented limitation: it has no
    #      effect when the size isn't known in advance, e.g. chunked
    #      transfer-encoding).
    #   6. only after the download is verified complete and within the cap
    #      is the temp file renamed (same directory, so the rename is
    #      atomic) to its final, freshly-sanitized name.
    [[ $# -eq 3 || $# -eq 4 ]] || usage
    ROOM_ID="$2"
    ATTACHMENT_ID="$3"
    DISPLAY_NAME="${4:-${ATTACHMENT_ID}.pdf}"

    _valid_bare_id "$ROOM_ID" \
      || { echo "error: room id must be a bare alphanumeric id, 1-64 characters (no path, no punctuation, no whitespace)" >&2; exit 1; }
    _valid_bare_id "$ATTACHMENT_ID" \
      || { echo "error: attachment id must be a bare alphanumeric id, 1-64 characters (no path, no punctuation, no whitespace)" >&2; exit 1; }

    mkdir -p -- "$BRAINARD_ATTACHMENTS_DIR"
    chmod 700 "$BRAINARD_ATTACHMENTS_DIR" 2>/dev/null || true
    BASE_RESOLVED="$(realpath -e -- "$BRAINARD_ATTACHMENTS_DIR")" \
      || { echo "error: cannot resolve attachments base dir: $BRAINARD_ATTACHMENTS_DIR" >&2; exit 1; }

    ROOM_DIR="$BRAINARD_ATTACHMENTS_DIR/$ROOM_ID"
    if [[ -e "$ROOM_DIR" && -L "$ROOM_DIR" ]]; then
      echo "error: refusing to fetch -- $ROOM_DIR exists and is a symlink, not a plain directory" >&2
      exit 1
    fi
    mkdir -p -- "$ROOM_DIR"
    chmod 700 "$ROOM_DIR" 2>/dev/null || true
    ROOM_DIR_RESOLVED="$(realpath -e -- "$ROOM_DIR")" \
      || { echo "error: cannot resolve room directory: $ROOM_DIR" >&2; exit 1; }
    case "$ROOM_DIR_RESOLVED" in
      "$BASE_RESOLVED"/*) ;;
      *)
        echo "error: refusing to fetch -- room directory resolved to $ROOM_DIR_RESOLVED, which is not strictly inside $BASE_RESOLVED" >&2
        exit 1
        ;;
    esac

    SAFE_NAME="$(_sanitize_filename "$DISPLAY_NAME")"
    FINAL_PATH="$ROOM_DIR_RESOLVED/$SAFE_NAME"

    TMPFILE="$(mktemp "$ROOM_DIR_RESOLVED/.fetch.XXXXXX")"
    HEADER_FILE="$(mktemp "$ROOM_DIR_RESOLVED/.fetch-headers.XXXXXX")"
    # Cleanup on any exit path (error, signal, or normal return) removes
    # both scratch files -- once the temp body is successfully renamed to
    # FINAL_PATH below, TMPFILE no longer exists at that path so this is a
    # harmless no-op; on any failure path it guarantees no partial download
    # is left behind under a name a caller could mistake for complete.
    trap 'rm -f -- "$TMPFILE" "$HEADER_FILE"' EXIT

    FETCH_URL="${BRAINARD_URL}/v1/rooms/${ROOM_ID}/attachments/${ATTACHMENT_ID}/download"
    DOWNLOAD_OK=1
    if ! printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
        | curl -sS -K - --max-time "$BRAINARD_FETCH_TIMEOUT_SECS" --path-as-is --globoff \
            --max-redirs 0 --max-filesize "$BRAINARD_FETCH_MAX_BYTES" \
            -D "$HEADER_FILE" "$FETCH_URL" \
        | head -c $((BRAINARD_FETCH_MAX_BYTES + 1)) > "$TMPFILE"; then
      DOWNLOAD_OK=0
    fi

    HTTP_CODE="$(awk 'NR==1{print $2; exit}' "$HEADER_FILE" 2>/dev/null || true)"
    ACTUAL_SIZE="$(wc -c < "$TMPFILE" 2>/dev/null || echo 0)"

    if [[ "$DOWNLOAD_OK" -ne 1 ]]; then
      echo "error: download failed (transport error, timeout, or size exceeded $BRAINARD_FETCH_MAX_BYTES bytes)" >&2
      exit 1
    fi
    if [[ "$HTTP_CODE" != "200" ]]; then
      echo "error: refusing response -- HTTP status was '${HTTP_CODE:-<none>}', not 200 (redirects and error responses are never followed or accepted)" >&2
      exit 1
    fi
    if [[ "$ACTUAL_SIZE" -gt "$BRAINARD_FETCH_MAX_BYTES" ]]; then
      echo "error: response exceeded the $BRAINARD_FETCH_MAX_BYTES byte cap; aborted, nothing written" >&2
      exit 1
    fi
    if [[ "$ACTUAL_SIZE" -eq 0 ]]; then
      echo "error: response body was empty; aborted, nothing written" >&2
      exit 1
    fi

    mv -f -- "$TMPFILE" "$FINAL_PATH"
    echo "fetched: $FINAL_PATH (${ACTUAL_SIZE} bytes)"
    ;;
  cleanup)
    # ADR-0012 decision 12: the agent names a room id and NOTHING else --
    # there is no argument through which it could ever supply a path to
    # delete, so a wrong path is unnameable, not merely rejected after the
    # fact. The delete is confined to $BRAINARD_ATTACHMENTS_DIR/<room-id>/
    # by construction (the id is validated before it forms any path, same
    # as `fetch`) and by verification (the resolved target must land
    # strictly inside the attachments base dir, and must not itself be a
    # symlink) before anything is removed. The base directory itself is
    # never a deletion target. Any anomaly (room slot is a symlink, room
    # slot is a plain file instead of a directory, resolved path escapes
    # the base dir) is refused outright rather than guessed past.
    [[ $# -eq 2 ]] || usage
    ROOM_ID="$2"
    _valid_bare_id "$ROOM_ID" \
      || { echo "error: room id must be a bare alphanumeric id, 1-64 characters (no path, no punctuation, no whitespace)" >&2; exit 1; }

    if [[ ! -e "$BRAINARD_ATTACHMENTS_DIR" ]]; then
      echo "nothing to clean up: attachments base dir does not exist ($BRAINARD_ATTACHMENTS_DIR)"
      exit 0
    fi
    BASE_RESOLVED="$(realpath -e -- "$BRAINARD_ATTACHMENTS_DIR")" \
      || { echo "error: cannot resolve attachments base dir: $BRAINARD_ATTACHMENTS_DIR" >&2; exit 1; }

    ROOM_DIR="$BRAINARD_ATTACHMENTS_DIR/$ROOM_ID"
    if [[ -L "$ROOM_DIR" ]]; then
      echo "error: refusing to clean up -- $ROOM_DIR is a symlink, not a plain directory; this is never expected, treating as an anomaly" >&2
      exit 1
    fi
    if [[ ! -e "$ROOM_DIR" ]]; then
      echo "nothing to clean up: no local directory for room $ROOM_ID"
      exit 0
    fi
    if [[ ! -d "$ROOM_DIR" ]]; then
      echo "error: refusing to clean up -- $ROOM_DIR exists but is not a directory; treating as an anomaly" >&2
      exit 1
    fi

    RESOLVED="$(realpath -e -- "$ROOM_DIR")" \
      || { echo "error: cannot resolve room directory: $ROOM_DIR" >&2; exit 1; }
    case "$RESOLVED" in
      "$BASE_RESOLVED"/*) ;;
      *)
        echo "error: refusing to clean up -- room directory resolved to $RESOLVED, which is not strictly inside $BASE_RESOLVED" >&2
        exit 1
        ;;
    esac
    if [[ "$RESOLVED" == "$BASE_RESOLVED" ]]; then
      echo "error: refusing to clean up -- resolved to the attachments base dir itself, never a valid deletion target" >&2
      exit 1
    fi

    # Delete only the room directory's own *contents*, never the directory
    # entry itself (so a concurrent `fetch` racing this has a stable
    # directory to mkdir -p against) and never the base dir. `rm -rf` never
    # follows a symlink argument to its target -- it unlinks the symlink
    # itself -- so a symlink planted *inside* the room dir cannot be used to
    # delete anything outside it either.
    find "$RESOLVED" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    echo "cleaned up room $ROOM_ID ($RESOLVED)"
    ;;
  *)
    usage
    ;;
esac
