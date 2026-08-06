# 0004 — Contract Rulings from the Five-Item Walkthrough

- **Status:** accepted
- **Date:** 2026-08-06

## Context and Problem Statement

With architecture (ADR-0002) and operating model (ADR-0003) fixed, the v1 contracts were reviewed with the owner item by item — checkpoint deposit, library entry, doctrine, projects, API surface — each judgment call discussed with edge cases before ruling. Full spec: `docs/spec/contracts-v1.md`.

## Decision

1. **Event kinds:** fixed nine-kind vocabulary, strictly enforced, with four contract-level mitigations (self-explaining rejections + relabel-to-`note` recovery, append-only vocabulary, hub-before-doctrine rollout, tag-based promotion signals). Loud recoverable failures chosen over silent retrieval decay.
2. **Handoff enforcement:** `session_end` deposits must carry a handoff note or an explicit `no_handoff` reason; silence rejected. Waivers logged; contradictions flagged.
3. **Metrics:** optional per-deposit `metrics` object; any subset valid; never enforced.
4. **Library namespaces:** exactly three — `lessons`, `howto`, `reference`; incidents fold into `lessons` as tags.
5. **Entry lifecycle:** `active` / `superseded` (server-set) / `retired` (explicit, reasoned). `supersedes` is an array (merges); forks allowed and flagged for the librarian.
6. **Body structure:** templates are doctrine guidance, never server validation; server checks only non-empty and size cap.
7. **Duplicates:** never blocked; server attaches similarity hints on arrival — the librarian's dedup queue.
8. **Doctrine writes:** owner-only — the single exception to full trust. AIs file inert proposals; owner promotes. Deposits stamp the doctrine version they ran under.
9. **Rule tiers:** global rules carry stable IDs and a tier — non-negotiable (immutable everywhere) or default (overridable per-project by ID); server rejects overlays touching non-negotiables.
10. **Bootstrap:** five components (compiled doctrine, project context + latest handoff, operating instructions, templates, capped lessons digest), markdown canonical, hard size budget, fetches logged.
11. **Identity:** per-machine tokens (confirmed; not per-session), stored hashed, shown once; owner root token for admin.
12. **UI (amends ADR-0003 §12):** read-only browse **plus an owner-gated admin area** — machine minting/revocation with last-seen view, doctrine proposal approvals.
13. **Projects:** thin auto-stubbed registry; immutable time-ordered handoff chain; **mirror model** for ADRs/docs — project repos stay canonical, deposits carry copies, the Brain makes them searchable fleet-wide.
14. **Search default scope:** library + decisions + handoffs; journal opt-in.

## Alternatives Considered

- Open event vocabulary — rejected: guaranteed synonym drift silently corrupts retrieval, the Brain's core job.
- Hard-reject session_end without handoff (no waiver) — rejected: forces junk ceremony notes that pollute continuity.
- Draft/verified entry states — obsolete under full trust; replaced by active/superseded/retired.
- Server-enforced body templates — rejected: validation forces presence, not quality; deposit-time prose rejections.
- Write-time interactive dedup (confirm-or-update) — rejected: impossible under atomic batch deposits at session end.
- Full-trust doctrine writes — rejected: corrupted doctrine disables the very sessions that would correct it; poison kills the antidote.
- Overlays override anything / nothing — rejected in favor of two-tier rules.
- Brain as canonical store for project docs, or pointers-only — rejected: breaks repo self-containment / makes decisions unreadable fleet-wide.
- Per-session tokens — rejected: minting friction against one-paste onboarding; session ids stay self-reported.

## Consequences

- The contracts are implementable without further owner input except two known build-time items: backup target machine; librarian runtime and schedule.
- ADR-0003's UI decision is amended by ruling 12.
- The librarian's inbox is fully specified by the contracts themselves: duplicate hints, fork flags, waiver contradictions, `lesson.candidate` harvest, recurring-tag promotion signals.
