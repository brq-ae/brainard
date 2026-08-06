# 0001 — Project Working Agreements: Model Roles, Git, ADRs

- **Status:** accepted
- **Date:** 2026-08-06

## Context and Problem Statement

At project start, the user (project owner) established ground rules for how the AI assistant works on brainard: which models do what, how progress is tracked, and how decisions are recorded.

## Decision

1. **Model roles:** Fable 5 is the orchestrator only. All execution — coding, reading, searching, running commands — is delegated to Sonnet 5 subagents. The user switched the session model to Fable 5 via `/model`.
2. **Ground rules:** No assuming, no guessing, no auto-executing. Ask the user when anything is unclear.
3. **Version control:** A local git repository at `/root/brainard`; all progress is committed with clear messages.
4. **Decision records:** All significant decisions are documented as MADR-style ADRs in `docs/adr/`, numbered sequentially.

## Alternatives Considered

- Keeping Opus 5 as the orchestrator — rejected; the user explicitly chose to switch the session to Fable 5.
- Full Nygard ADR template or a custom minimal template — rejected in favor of MADR-style, which is lightweight and widely used.
- No formal decision log — rejected; the user requires documented decisions.

## Consequences

- Every unit of work carries delegation and documentation overhead, in exchange for traceability and explicit user control.
- Significant decisions are blocked until their ADR is agreed, preventing silent scope drift.
