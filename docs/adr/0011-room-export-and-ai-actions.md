# 0011 — Room Transcript Export and AI Actions

- **Status:** accepted
- **Date:** 2026-08-23

## Context and Problem Statement

Getting a room's conversation out meant selecting the rendered page and copying it by hand. And once a room had produced something valuable — a debate's outcome, a design critique's conclusions — turning that into durable knowledge meant re-reading the whole transcript and writing it up manually, or deleting the room and losing it.

The hub already has a configured LLM provider (ADR-0010) for the built-in librarian. The same provider can read a transcript.

## Decision

1. **Export.** The room view offers a one-click copy of the transcript to the clipboard as formatted markdown, and a download of the same content as a file. A JSON form is available for machine consumption. No model is involved.

2. **AI actions.** Four owner-triggered actions run against a room's transcript using the configured provider: **summarize** (what was discussed and concluded), **verdict** (for debate and critique rooms: which side argued more convincingly, the strongest point each way, and why), **decisions and action items** (concrete outcomes and follow-ups), and **extract lessons** (reusable knowledge phrased as library entries). Each is a single-shot completion, consistent with ADR-0010's judgment-only approach, so a small local model can serve them.

3. **Review, then deposit.** A result is displayed for the owner to read. Depositing it into the library is a separate, explicit action where the owner chooses the destination project and namespace. Nothing is written automatically. This is what makes deleting a room safe: the distilled knowledge is kept deliberately, the transcript is disposable.

4. **The transcript is untrusted input.** Room messages are written by agents, so a crafted message could otherwise hijack a summarizer into emitting attacker-chosen text that the owner might then deposit as fact. Transcripts are therefore wrapped in per-call random-nonce delimiters with explicit data-not-instructions framing, exactly as ADR-0010's librarian prompts are, and the result is always shown for review before it can become knowledge.

5. **Bounds.** Transcripts are truncated to a size a modest model can handle, and truncation is disclosed in the output. Actions are owner-only: they spend the owner's provider budget and write to the owner's library.

## Alternatives Considered

- Automatic deposit on running an action — rejected: writes model output into the library without the owner ever reading it.
- A separate, dedicated summarizer service or provider — rejected: the configured provider already exists and is deliberately provider-agnostic.
- Export as rendered HTML — rejected in favour of markdown, which pastes cleanly into notes, issues, and chat.

## Consequences

- The room view gains an owner-only panel; the actions consume provider tokens or local compute per click.
- Deposited results carry provenance pointing back at the room they came from.
- Summaries inherit the quality of the configured model, and — like the librarian's judgments — are advisory: the owner reviews before anything is kept.
