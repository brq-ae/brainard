# 0009 — Mid-session Room Mode Switching (extends ADR-0007)

- **Status:** accepted
- **Date:** 2026-08-22

## Context and Problem Statement

A room's mode (debate/collaborate/brainstorm/critique/freeform) is set at creation and baked into each agent's join prompt. Owners want to change the mode mid-conversation — e.g. run a critique, then switch to a debate — without tearing down the room. Running agents won't spontaneously adopt a new stance, so a switch has to be communicated to them.

## Decision

1. **Owner-only switch.** Agents may *suggest* a mode change in the room chat; the owner triggers the actual switch (from the live room view). Room-rule changes stay owner-controlled, consistent with the doctrine.
2. **Mechanism.** A switch updates the room's `mode`/`topic` and, for an asymmetric target mode, the members' `side` assignments — all under the room's `SELECT ... FOR UPDATE` row lock (the same discipline every other room mutator uses). It then posts a `kind='system'` announcement stating the new mode and each agent's new stance (drawn from the single-source mode definitions). Agents, polling, see the announcement and adapt; the join prompt primes them to watch for a mode-switch announcement and to suggest a switch when useful.
3. **Constraints.** Only an open room can switch; a non-freeform target requires a topic; an asymmetric target requires the two sides assigned to the two members. The system announcement counts as a message toward the room's cap.
4. **Lightweight alternative retained.** The owner (or an agent) simply posting a free-text "let's switch to X, here are the stances" message already makes agents adapt and needs no feature; this decision *formalizes* it so the room's mode, sides, and UI reflect the change.

## Alternatives Considered

- Agents switch the mode autonomously — rejected for v1 (kept owner-controlled; agents suggest). May revisit.
- Message-only, no feature — viable and documented, but doesn't update the room's stored mode/sides or the UI.

## Consequences

- Adds a fifth room mutator that takes the row lock — reinforcing the standing backlog item for a shared "with the room row locked" helper so a new mutator can't forget it.
- Agents must be primed (via the join prompt) to honor a mid-session mode-switch announcement.
