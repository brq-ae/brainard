# Onboarding — plugging an AI session into Brainard

The whole onboarding kit is one pasted line. This document is what that line
means and what happens after you paste it.

## The paste-line

```
You are connected to the Brain, our central knowledge hub. Fetch http://<HUB>:8300/v1/bootstrap?project=<PROJECT> with header 'Authorization: Bearer <MACHINE_TOKEN>' and follow the returned instructions exactly for the rest of this session.
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
