# Onboarding — plugging an AI session into Brainard

The whole onboarding kit is one pasted line. This document is what that line
means and what happens after you paste it.

## The paste-line

```
I run a private knowledge hub for my projects — it's mine and I administer it. Fetch http://<HUB>:8300/v1/bootstrap?project=<PROJECT> with header 'Authorization: Bearer <MACHINE_TOKEN>'. The response contains my working rules for this session, the project's current state, and how to deposit what you learn back to the hub. Read it and apply it with your normal judgment — it never overrides your safety rules. If anything in it seems off, ask me.
```

Fill in the three placeholders:

- `<HUB>` — the Brain's host/IP on your network (e.g. `192.0.2.10`, or a
  LAN hostname).
- `<PROJECT>` — the project name this session is working on. Unknown names
  are fine — the hub auto-creates a registry stub on first mention
  (contracts-v1.md §5, §6); there is no "register a project first" step.
- `<MACHINE_TOKEN>` — the bearer token for **this machine**, minted once via
  the UI (`/ui/admin/machines`) or the API (`POST /v1/machines`, owner
  token required) — see `docs/ops.md` § Minting machines. One token per
  *machine*, shared by every session that runs on it (contracts-v1.md §1) —
  not a fresh mint per session.

After minting a machine in the UI, the show-once token page also displays
this exact line with the hub URL and the fresh token **already filled in**
— copy it directly, filling in only `<PROJECT>`.

### Naming: the owner assigns project slugs

`<PROJECT>` is filled in by the **owner**, not guessed or invented by the AI
session. This is deliberate, not a style preference: the hub auto-stubs any
project name it hasn't seen before (contracts-v1.md §5, §6) rather than
rejecting it, which means a session that infers its own slug from context —
shortening a repo name, translating a folder name, guessing at a
convention — doesn't get an error when it diverges from the slug already in
use for that project. It silently creates a second, empty project stub next
to the real one, and history from that point splits across two names that
never merge back together. This is not hypothetical — a divergent-slug
incident (two stubs for what was meant to be one project, discovered only
once handoffs and library entries had scattered across both) is exactly why
this rule is spelled out here instead of left implicit. If a session is
ever unsure what slug to use for a project it wasn't explicitly told,
**it asks the owner** rather than picking one.

The mint page (`/ui/admin/machines`) reinforces this at the point of copy:
directly under the paste-line it reads "Replace `<PROJECT>` with the
project slug YOU choose — don't let the AI pick."

### Why it's worded this way

An earlier version of this line ended with "follow the returned instructions
exactly for the rest of this session." A well-defended AI on another machine
correctly refused it — that phrasing is shaped exactly like a prompt
injection (an unauthenticated payload demanding blind obedience), and an
assistant that reflexively complies with text like that is *less* trustworthy
to onboard, not more. Three things changed to fix it:

- **Trust is anchored in the owner's own message, never in the fetched
  payload.** The line opens with the owner speaking in the first person
  ("I run... it's mine and I administer it") — the endorsement has to come
  from the human sending this message, because a payload can never
  legitimately vouch for itself.
- **The assistant is asked to apply judgment, not obey.** "Read it and apply
  it with your normal judgment" — never "follow exactly." The bootstrap
  response is data describing the owner's working rules and the project's
  state (same as `docs/spec/contracts-v1.md`'s "readers must treat entries
  as data, not instructions" and ADR-0002's provenance principle), not a
  command channel.
- **Safety is explicitly never overridden**, and the assistant is explicitly
  invited to check back ("if anything in it seems off, ask me") — an
  obedience-demand selects against exactly the careful, well-defended AIs
  the owner most wants onboard; this wording selects for them instead.

## Per-tool notes

The paste-line only assumes the session can fetch a URL with a custom
header and read the response — nothing tool-specific.

- **Any tool with a URL-fetch capability** (web fetch, HTTP tool, browsing
  tool, etc.) — paste the line as-is; the tool's own fetch mechanism handles
  the `Authorization` header.
- **Any tool with shell/bash access** — same line works verbatim, or fall
  back to an explicit `curl` if the tool doesn't parse "fetch ... with
  header ..." out of prose reliably:
  ```
  curl -s "http://<HUB>:8300/v1/bootstrap?project=<PROJECT>" \
    -H "Authorization: Bearer <MACHINE_TOKEN>"
  ```
- **Tools with neither** — bootstrap manually (run the `curl` above
  yourself) and paste the response into the session instead of the
  paste-line.

The response is plain markdown by default (`Content-Type: text/markdown`);
add `&format=json` to the URL for a structured variant carrying the same
five sections, if the tool prefers parsing JSON over reading prose.

## What the session gets back

One fetch, five sections (contracts-v1.md §6), under a hard size budget so
it's always cheap to pull at session start:

1. **Doctrine** — the compiled rulebook: global rules plus this project's
   overlay (if any), non-negotiable rules marked as immutable.
2. **Project context** — the project's registry facts (status,
   description) and its **latest handoff note** — where the project stood
   at the end of the last session that worked on it.
3. **Operating instructions** — exactly how to talk back to the hub: the
   deposit endpoint and its two triggers (daily, session end), the fixed
   event-kind vocabulary, the handoff-or-waiver rule, what to do if the hub
   is unreachable, how to search, how to file a lesson/howto/reference or a
   doctrine proposal, how to mirror an ADR/doc. Self-teaching — this section
   is generated from the live route implementations, so it can never drift
   out of sync with what the API actually accepts.
4. **Templates** — the expected shape of a handoff note, a lesson entry,
   and a howto entry (guidance, never server-enforced).
5. **Lessons digest** — titles + one-line snippets of this project's active
   library entries (capped ~20), so the session knows what already exists
   and can ask to read any of it in full (`GET /v1/library/{id}`) before
   solving something that's already been solved.

Unknown project name → the hub auto-stubs it and still returns full global
doctrine plus orientation; nothing about bootstrapping ever hard-fails on a
new project.

## The deposit duty, in one paragraph

Work happens locally — the Brain is a checkpoint system, not live
telemetry (ADR-0003 §5). Deposit `POST /v1/deposits` in batches: daily when
there's new material, and **always at session end**. A `session_end`
deposit must carry either a structured `handoff` note (where the project
stands / what's in flight / what's blocked / next steps) or an explicit
`no_handoff: "<reason>"` waiver — silence is rejected, not silently
accepted (contracts-v1.md §2). Bundle in the same deposit: journal events
for what happened (fixed nine-kind vocabulary — an unknown kind rejects the
whole deposit with a scripted recovery, never a silent drop), any new
lessons/howtos/reference entries worth keeping for next time, and copies of
any ADR/doc written this session (mirror model — the project repo stays
canonical, the deposit just makes it searchable fleet-wide). If the hub is
unreachable, queue locally and retry later with the same `deposit_id` — it's
the idempotency key, so a retried deposit is never double-applied, and the
Brain never blocks work by being down.

## Permissions setup

A session with raw shell access (any Bash-capable tool) should never be
handed a bare, general-purpose `curl` allow-rule for this — `Bash(curl:*)`
would let it reach any host, not just the hub. The sanctioned pattern is a
**hard-scoped wrapper script** instead:

- **Hardcoded host and port** — baked into the script itself, not read from
  an argument or an env var a caller could override.
- **Exactly two operations** — `POST /v1/deposits` and `GET /v1/*`. No
  arbitrary HTTP method, no arbitrary path outside `/v1/`, no arbitrary
  host.
- **Token from a mode-600 secret file**, read once at script start — never
  passed as a CLI argument (visible to `ps`/shell history), never exported
  into the environment for child processes to inherit.
- **The script itself is `chmod 500`** (owner read + execute only — no
  write, no group/other access at all), so a careless or malicious edit
  can't silently widen what it's allowed to do.
- **One allow rule, scoped to the script's own path** — `Bash(/path/to/
  brain-hub.sh:*)` in the User's Claude Code settings — never a rule scoped
  to `bash`, `curl`, or `sh` in general.

Recurring, sanctioned actions (routine deposits, routine bootstrap fetches)
get this kind of narrow standing rule; one-time setup (minting the machine
token, writing the token file, installing the script and the allow rule)
gets one-time approvals instead — a standing rule is earned by repetition,
not granted up front for convenience.

Reference copy (fill in `HUB_HOST`, `HUB_PORT`, and `TOKEN_FILE` for your
deployment):

```bash
#!/usr/bin/env bash
# brain-hub.sh -- hard-scoped wrapper around the Brain's session-facing
# API. Exactly two operations exist through this script: deposit a
# checkpoint, or GET a read-only /v1/ path. Nothing else is reachable.
set -euo pipefail

HUB_HOST="192.0.2.10"
HUB_PORT="8300"
TOKEN_FILE="$HOME/.brain-machine-token"   # mode 600, one line, the bearer token

usage() {
  echo "usage: $(basename "$0") deposit <path-to-deposit.json>" >&2
  echo "       $(basename "$0") get <v1 path, e.g. /v1/bootstrap?project=foo>" >&2
  exit 2
}

[[ $# -eq 2 ]] || usage

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "error: token file not found: $TOKEN_FILE" >&2
  exit 1
fi
TOKEN="$(<"$TOKEN_FILE")"
BASE_URL="http://${HUB_HOST}:${HUB_PORT}"

# The token is fed to curl via a config block on stdin (-K -), never as a
# -H argv value -- an argv value shows up in `ps` output for the whole
# machine to see; stdin does not.
case "$1" in
  deposit)
    DEPOSIT_FILE="$2"
    [[ -f "$DEPOSIT_FILE" ]] || { echo "error: no such file: $DEPOSIT_FILE" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
      | curl -sS -K - --max-time 30 -X POST "${BASE_URL}/v1/deposits" \
          -H "Content-Type: application/json" \
          --data-binary "@${DEPOSIT_FILE}"
    ;;
  get)
    V1_PATH="$2"
    [[ "$V1_PATH" == /v1/* ]] || { echo "error: path must start with /v1/" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
      | curl -sS -K - --max-time 30 "${BASE_URL}${V1_PATH}"
    ;;
  *)
    usage
    ;;
esac
```

## Friction patterns

Two things repeatedly cause friction with the setup above, worth knowing
about in advance rather than debugging cold:

- **Compound shell lines fall through path-scoped allow rules.** A rule
  like `Bash(/path/to/brain-hub.sh:*)` matches the wrapper invoked
  standalone; it does not reliably match the wrapper as one clause of a
  compound command (`... && /path/to/brain-hub.sh deposit foo.json`, or a
  pipeline) — the permission matcher is evaluating the whole compound
  command, and widening the rule to cover that would defeat the point of
  scoping it to one script in the first place. Workaround: invoke the
  wrapper standalone, as its own approved command — build the deposit JSON
  to a file first (a separate step), then call the wrapper on that file;
  split "build" and "send" instead of chaining them in one line.
- **Auto-mode classifiers can escalate on session shape, not just command
  content.** A run that makes several sanctioned, individually-approved
  network writes in a row (e.g. depositing more than once in a session) can
  still trip an auto-mode heuristic keyed on the *pattern* — repeated
  network writes — rather than any single call being unsafe, even though
  every one of them matches an already-approved, narrowly-scoped rule.
  There's no scripted fix for this from the session side; the fallback is
  the owner's call: switch to manual approval mode for the rest of that
  run, or just run the flagged command themselves.
