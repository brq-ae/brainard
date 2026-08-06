# 0003 — The Brain: Operating Model

- **Status:** accepted
- **Date:** 2026-08-06

## Context and Problem Statement

ADR-0002 fixed the architecture (HTTP API, layered storage, Docker Compose). A structured brainstorm with the owner then refined *how the Brain operates*: who reads it, when sessions write, how trust and corrections work, and which supporting capabilities matter. Those decisions are recorded here; the full narrative lives in `docs/vision.md`.

## Decision

1. **Audience:** machine-first — AIs are the primary readers; the owner's UI is secondary (read-only browse).
2. **Purpose:** three jobs — teach doctrine, carry project context across sessions (handoff notes), grow a never-solve-twice library. Full activity is also journaled.
3. **Onboarding:** one pasted line (hub URL + machine token) into any new session; the session fetches doctrine and project state itself. No per-machine installation required.
4. **Doctrine layering:** one global rulebook plus optional per-project overlays.
5. **Write model — checkpoints, not telemetry:** sessions accumulate activity locally and deposit in batches: daily when new material exists, and always at session end. No per-step hub calls.
6. **Read model:** fetch at session start; re-read on the owner's command; at a block, ask the owner before checking the Brain (and check the Brain before the internet).
7. **Offline behavior:** hub unreachable → continue working, queue deposits, retry later. The Brain never blocks work.
8. **Trust:** writes are trusted at full weight immediately — no review gate.
9. **Corrections:** supersede, never erase. No in-place edits; history is permanent.
10. **Identity:** per-machine bearer tokens — honest provenance, per-machine revocation.
11. **Backups:** nightly pg_dump + git mirror pushed to a second machine on the LAN.
12. **UI:** read-only browse (search library, view projects/handoffs/activity), served by the same app.
13. **Librarian:** fully autonomous scheduled curation (dedup, merge, organize, harvest lesson candidates); every action is a supersession and therefore reversible.

## Alternatives Considered

- Per-step live logging — rejected: noisy, interrupts work; owner explicitly wants deposits only periodically or on command.
- Milestone-triggered or 30-minute syncs — rejected in favor of daily + session-end.
- Human review gates on writes (owner promotes/verifies) — rejected: owner chose full trust, zero friction.
- Autonomous mid-task hub reads — rejected: at blocks the session must ask the owner first.
- One shared access token — rejected in favor of per-machine tokens (provenance, revocation).
- In-place edits of wrong entries — rejected in favor of supersession.
- Propose-then-approve librarian — rejected: owner chose full autonomy, accepting reversibility as the safety net.
- Stop-work-when-hub-down — rejected: the Brain must never be a single point of failure for getting work done.

## Consequences

- The API must be shaped around batch deposits (checkpoint uploads), handoff notes as first-class objects, and doctrine endpoints — the v1 contracts must reflect this.
- Full-trust writes mean a wrong entry propagates at full weight until superseded; the librarian and easy supersession are the correction path.
- Per-machine tokens require a small registration step per machine.
- Open inputs before build completes: backup target machine; librarian runtime and schedule.
