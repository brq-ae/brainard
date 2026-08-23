# The librarian — curating the Brain

The librarian is a **role**, not a specific product: something that
periodically works the library's queues and tidies things up. Two
independent implementations of that role ship with Brainard; this document
explains both, the raw API contract underneath either one, and how to pick
between them. See `docs/adr/0010-pluggable-librarian-runtimes.md` for the
design rationale behind supporting both.

## What the librarian does

Every run works the same three queues, in order (the exact judgment rules
are spelled out in `scripts/librarian-prompt.md`, which both runtimes below
follow — the built-in engine encodes the same rules in Python, the external
agent reads that file directly as its working prompt):

1. **Dedup/merge flags.** `POST /v1/deposits` raises a `duplicate` flag when
   a new library entry looks like it might restate an existing one (cheap
   full-text similarity), and a `fork` flag when two entries both supersede
   the same parent. The librarian fetches both sides of each flag, judges
   whether they're genuinely the same knowledge, and either deposits one
   merged entry (`supersedes` naming both parents) or resolves the flag as
   distinct. **Conservative by design**: when unsure, it leaves the entries
   alone — a wrong merge destroys information, a flag left open just waits
   for the next run.
2. **Harvest lesson candidates.** Sessions can log a `lesson.candidate`
   journal event when something worth remembering happens but there's no
   time to write it up properly mid-session. The librarian reads the queue,
   checks (via search) whether an existing entry already covers it, and if
   not, writes a proper `lessons`-namespace entry using the standard
   template (Situation / Problem / Fix / Why it works).
3. **Summarize.** Every run ends with one deposit containing a `note` event
   summarizing what happened — flags resolved, lessons written, any
   projects that have gone quiet (no deposit in 7+ days).

Everything the librarian writes lands as a **supersession or a fresh
entry** — never an edit, never an erasure. A bad merge is reversible (the
history is still there); that safety net is why full autonomy (no
human-in-the-loop review of its output) is an acceptable trade.

Doctrine and doctrine-proposal entries are off-limits to both runtimes: the
librarian never writes, approves, or rejects doctrine, and it never targets
a proposal entry with a merge, retire, or supersede action of its own —
proposals close only through the owner's review.

## Two ways to run it

### A. Built-in (zero setup)

Configure an LLM provider at `/ui/llm`: base URL, model, and an optional
API key. Any OpenAI-compatible chat-completions endpoint works — Ollama,
OpenAI, OpenRouter, LM Studio, vLLM, or a compatible gateway in front of
another provider. Once a provider is set, the librarian runs automatically
on its own schedule, entirely inside the application process. History and
a manual trigger are at `/ui/librarian`.

This path needs **no CLI, no cron, no machine token, no shell access on
the host** — it's pure application code. Orchestration (which flags to
look at, which events to harvest, how to build and send the deposit) is
deterministic Python that calls the same domain functions any deposit goes
through; the LLM is consulted only for a single, narrow judgment call per
item ("are these two entries duplicates, and if so what's the merged
text?" / "is this event worth turning into a lesson?") — never a
multi-turn, tool-using conversation. That makes it cheap, predictable, and
usable even with a small local model that couldn't sustain an agentic loop.

**Kill switch**: the built-in librarian writes under one reserved machine
identity, `brainard-librarian`, shown in Admin → Machines like any other
machine. Revoking it stops every subsequent run cleanly (no LLM call, no
deposit) until it's reactivated — the same Revoke/Reactivate controls used
for any other machine token.

### B. External agent (full tool use)

An agent CLI (Claude Code or any other tool-using agent) runs unattended on
a schedule, driven by:

- **A machine token** — minted like any other machine (`docs/ops.md` §
  "Minting machines"), saved to a local token file.
- **`scripts/brain-wrapper.sh`** — the hard-scoped wrapper the agent's
  shell access is restricted to. It exposes exactly three operations
  (`get`, `deposit`, `resolve` — see below) and nothing else; this is the
  entire enforcement boundary for what the agent can do to the hub.
- **`scripts/librarian-prompt.md`** — the agent's working prompt: identity,
  the three-phase loop above, deposit envelope conventions, and the
  standing "when unsure, treat as distinct" rule.
- **`scripts/librarian-run.sh`** — the cron entrypoint. Clears/prunes the
  outbox, checks preconditions, invokes the agent with the prompt, logs the
  run, prunes old logs, and pings `notify-me` with the outcome.

This path is more capable — a real tool-using agent can page through
results, retry, search iteratively, and reason across multiple steps before
deciding — at the cost of installing a CLI, minting a token, and wiring up
cron.

**Bring your own agent CLI**: `scripts/librarian-run.sh` defaults to
invoking `claude` (the same command the author's own deployment cron uses),
but this is overridable. Set `LIBRARIAN_AGENT_CMD` to any command line —
the script runs it via `bash -c` and pipes the librarian prompt to its
**stdin** instead of passing it as an argument. The contract for a
non-Claude runtime:

- Read the prompt from stdin.
- Have `scripts/brain-wrapper.sh` (or an equivalent hard-scoped wrapper)
  reachable — on `PATH`, or by an absolute path the command already knows.
- Exit non-zero on failure, so `librarian-run.sh`'s own logging and
  `notify-me` ping still reflect the outcome.

Leaving `LIBRARIAN_AGENT_CMD` unset reproduces the default `claude`
invocation exactly — existing deployments are unaffected by the variable's
existence. Setting it to an empty or whitespace-only string is treated as a
configuration error (the run fails loudly, non-zero exit) rather than a
silent no-op success — `bash -c ""` would otherwise exit 0 immediately
without running any agent at all.

**Kill switch**: revoke the machine token minted for the external agent
(Admin → Machines, or `POST /v1/machines/{id}/revoke`); every call the
wrapper makes then fails, regardless of whether the cron line is still
installed. Removing the crontab line stops new runs from starting at all.

### When to choose which

| | Built-in | External agent |
|---|---|---|
| Setup | Configure a provider in the UI, done | Install a CLI, mint a token, wire up cron |
| Model | Any OpenAI-compatible endpoint, including small local models | Whatever the agent CLI itself uses |
| Reasoning | Single-shot judgment per item, deterministic orchestration | Full multi-step tool use |
| Cost/predictability | Cheap, bounded, easy to test | Depends on the agent/model |
| Good fit | Small local models, "just make it work," conservative single-shot calls are enough | Richer reasoning wanted, already running an agent CLI fleet, comfortable with more moving parts |

Both write through the exact same guardrails (supersede-never-erase, the
proposal boundary, per-run caps) — switching between them, or running
neither, changes nothing about what's safe to do to the library.

## Bring your own model / any language: the raw API contract

Nothing above is required to implement the librarian role — it only needs
an HTTP client and a bearer token. This is the minimum contract:

**1. List unresolved flags:**

```
GET /v1/flags?unresolved=true
Authorization: Bearer <machine token>
```

Each result has `type` (`duplicate` or `fork`), `entry_id` (the newer
entry), and `related_entry_id` (the one it might duplicate, or the fork
sibling). Page with `cursor` until `next_cursor` is null.

**2. Fetch each side in full:**

```
GET /v1/library/{id}
```

Read `status` — if either side is no longer `active` (already superseded
or retired by a prior action), the flag is stale; resolve it without
depositing anything.

**3. Resolve a flag once judged:**

```
POST /v1/flags/{flag_id}/resolve
```

Idempotent, per-flag-id. A merge deposit does not auto-resolve anything —
always follow it with an explicit resolve.

**4. Walk the lesson-candidate harvest queue:**

```
GET /v1/events?kind=lesson.candidate&limit=200
```

For each event, search first (`GET /v1/search?q=<keywords>`) to check
whether an active entry already covers it before writing a new one. There
is no "resolve" for events — writing the lesson entry is the completion; a
future run's search finds it and skips the event next time.

**5. Deposit — merges, harvested lessons, and the run summary all go
through the same endpoint:**

```
POST /v1/deposits
Content-Type: application/json
```

A minimal merge deposit (resolving a `duplicate` or `fork` flag by
combining two entries into one, `supersedes` naming both parents):

```json
{
  "deposit_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  "tool": "my-librarian",
  "session": "my-librarian-2026-08-23",
  "project": "brainard",
  "reason": "manual",
  "client_ts": "2026-08-23T03:30:00Z",
  "knowledge": [
    {
      "title": "Merged: connection pool sizing under load",
      "namespace": "lessons",
      "body": "## Situation\n...\n\n## Problem\n...\n\n## Fix\n...\n\n## Why it works\n...",
      "tags": ["postgres", "pooling"],
      "project": "brainard",
      "supersedes": ["01ENTRYAAAAAAAAAAAAAAAAAAA", "01ENTRYBBBBBBBBBBBBBBBBBBB"]
    }
  ]
}
```

Notes on the fields that trip people up:

- `deposit_id` must be a syntactically valid ULID (26 Crockford-Base32
  characters, first character `0`–`7`) and unique per deposit — it's the
  idempotency key; reusing one just replays the original ack.
- `knowledge[].supersedes` is what makes this a merge rather than a fresh
  entry: both parent ids go here, and the server transitions both from
  `active` to `superseded` atomically with the insert.
- `knowledge[].project`: set it **explicitly** on the item, not just on the
  envelope — the envelope's `project` (`"brainard"` above) is bookkeeping
  for the deposit as a whole; the item's own `project` is what the merged
  or harvested knowledge is actually filed under. The example above uses
  `"brainard"` on the item too, matching the envelope, purely so it POSTs
  successfully on a brand-new instance with no other registered projects —
  in a real merge, set it to whatever project the merged knowledge actually
  belongs to. Send `null` for knowledge that's genuinely universal.
- **An explicit item-level `project` naming something other than the
  envelope's own `project` must already be a registered project** — the
  server does not auto-create one from inside `knowledge[]` the way it
  auto-stubs the envelope's own project. Omit the `project` key entirely to
  inherit the envelope's project, or send `null` for universal knowledge,
  if the target project isn't already known to exist.
- `reason` is `"manual"` for a librarian run (it isn't a session with a
  start/end, so `"session_end"`'s handoff-or-waiver requirement doesn't
  apply).

A run-summary deposit (step 3 of the loop, "Summarize") is the same
endpoint with an `events[]` entry instead of `knowledge[]` — one `note`
event per run, tagged `librarian-run`.

That's the entire surface: five endpoints (`GET /v1/flags`,
`GET /v1/library/{id}`, `POST /v1/flags/{id}/resolve`,
`GET /v1/events`, `POST /v1/deposits`), one bearer token, plain JSON over
HTTP. `docs/spec/contracts-v1.md` documents the full deposit envelope for
edge cases not covered above (retiring an entry outright, mirroring a
document, a project-registry update riding along).

## Safety notes

Both runtimes are bound by the same guardrails, enforced server-side
regardless of which one (or neither) is running:

- **Conservative merging.** The standing rule for both is: when unsure
  whether two entries are genuine duplicates or fork siblings versus
  distinct, resolve as distinct and move on. A bad merge is much more
  costly than a flag sitting open for another day.
- **Supersede, never erase.** Every correction is a new version pointing
  back at what it replaces (`supersedes[]`); nothing is ever deleted or
  edited in place. History is always there to check or roll back to.
- **Doctrine and proposals are off-limits.** Neither runtime ever writes
  doctrine, and both are structurally prevented from merging or
  superseding across the proposal boundary — proposals stay inert against
  the live library until the owner approves or rejects them.
- **Caps.** Both runtimes bound a single run: a maximum number of flags and
  lesson candidates processed, a maximum number of LLM calls (built-in
  only — the external agent's own tool-use budget is whatever its CLI
  enforces, e.g. `--max-budget-usd` on `scripts/librarian-run.sh`'s default
  `claude` invocation), and a hard stop after repeated provider failures in
  a row. A capped-out run still writes its summary and leaves the rest of
  the queue for next time — nothing is lost, just deferred.

