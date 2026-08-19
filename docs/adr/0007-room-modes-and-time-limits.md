# 0007 — Room Modes and Time Limits (extends ADR-0006)

- **Status:** accepted
- **Date:** 2026-08-19

## Context and Problem Statement

Building on agent chat rooms (ADR-0006), the owner wants to optionally give a room a purpose — a **mode** with a **topic** — and a **time limit**. Modes: freeform (default), debate (for/against), collaborate, brainstorm/ideate, critique/red-team. The owner also wants to cap a room's wall-clock duration (e.g. "debate for 30 minutes" or "run for 10 hours").

## Decision

1. A room gains optional **mode** (`freeform` default | `debate` | `collaborate` | `brainstorm` | `critique`), **topic** (text), and a **deadline** (`expires_at`). Non-freeform modes require a topic. Asymmetric modes (`debate`, `critique`) assign a **side** per member (For/Against, Proposer/Critic); symmetric modes (collaborate, brainstorm) do not.
2. **Mode definitions are single-sourced** (like machine roles). Each mode maps to per-side role text plus a closing-statement wrap-up instruction, all injected into the join prompt (with the topic and the deadline filled in). The anti-injection framing from ADR-0006 is preserved.
3. **Time limit** is enforced by a lightweight always-on **background sweeper** task in the app (runs about every 60s): it closes rooms past `expires_at` (close_reason `time`) via the same atomic close path as the cap, fires the owner notification, and — shortly before the deadline — posts a `system` "post your closing statements" nudge into the room (guarded so it posts once). Lazy close-on-access alone was rejected: an idle expired room would not close or notify until touched.
4. The message cap (ADR-0006) remains an **independent second backstop**; whichever of time or count fires first closes the room. Either, both, or neither may be set.
5. The live view shows the mode/topic and a countdown. The owner remains the judge (two-agent v1; a moderator/judge agent is deferred to multi-agent v2).

## Alternatives Considered

- Lazy close-on-access only (no sweeper) — rejected: unreliable for idle/unattended expired rooms.
- Editing ADR-0006 in place — rejected: committed ADRs are immutable; this ADR extends it.
- A third moderator/judge agent — deferred to multi-agent v2.

## Consequences

- Introduces the **first always-on background task** in the Brain (the sweeper). It must be resilient (each cycle wrapped so an error is logged and never crashes the app; a fresh DB session per cycle), and it assumes the single-worker deployment (one sweeper instance; a multi-worker future would need a lock).
- The time-based close is a guardrail parallel to the cap and uses the same atomic, row-locked close path.
- Modes are prompt-shaping only; room mechanics and existing guardrails are unchanged.
