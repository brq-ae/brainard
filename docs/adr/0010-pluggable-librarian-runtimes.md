# 0010 — Pluggable Librarian Runtimes: Built-in LLM Client and External Agents

- **Status:** accepted
- **Date:** 2026-08-23

## Context and Problem Statement

The librarian — the autonomous curation role that dedups, merges, and harvests knowledge — was implemented as an external agent: a host cron job invoking the Claude Code CLI, which talks to the API through a hard-scoped wrapper script using a machine token. That works for the author's deployment but is a poor first experience for anyone who downloads Brainard: the wrapper script was never shipped in the repo (it existed only as an inline example in the onboarding docs), the runner hardcodes one specific CLI and its flags, and there is no documentation explaining what the librarian is or how to run one. It also makes the librarian *look* Claude-specific when the role is model-agnostic in principle.

## Decision

Support **two librarian runtimes**, both writing through the same domain functions and guardrails.

1. **Built-in (the default, zero-setup path).** The owner configures an LLM provider in the UI: base URL, model, and an optional API key. Any OpenAI-compatible endpoint qualifies — Ollama, OpenAI, Anthropic-compatible gateways, OpenRouter, LM Studio, vLLM. The librarian then runs inside the application on its own schedule.

   Critically, the in-app librarian is **not** an agentic tool-use loop. Orchestration is deterministic Python — the application already *is* the API, so it fetches flags, entries, and events and performs deposits by calling its own domain functions directly. The LLM is used only for **judgment**: "are these two entries duplicates, and if so what is the merged entry?" This single-shot pattern works on small local models that could not sustain a tool-using agent loop, is cheaper, more predictable, and far easier to test.

2. **External agent (the powerful path).** An agent CLI with a machine token, the librarian prompt, and HTTP access — full tool use and richer reasoning. To make this usable by others, the hard-scoped API wrapper ships in the repo (parameterized by environment, not hardcoded paths), the runner becomes runtime-agnostic via an agent-command variable that defaults to the current Claude Code invocation, and a dedicated librarian document explains both paths and the raw API calls needed to implement the role in any language.

3. **Credential handling.** Brainard is designed for a private LAN, single owner. The provider API key is stored in the database, owner-only, masked in the UI after saving, never logged and never served at bootstrap. It is *not* encrypted at rest: an encryption key living in the same environment file on the same host is security theatre, and it introduces a silent-breakage failure mode on key loss or rotation. Two mitigations are documented instead: an environment variable takes precedence over the stored value and stores nothing in the database, and a local model such as Ollama needs no key at all. The documentation states plainly that database backups will contain the key.

## Alternatives Considered

- **Built-in only** — rejected: loses the richer tool-using agent path the author uses.
- **External only (ship the wrapper and docs, no LLM client)** — rejected as the default: still requires installing a CLI, minting a token, and configuring cron before the flagship autonomous feature does anything.
- **A full agentic loop inside the application** (function-calling against the provider) — rejected: substantially more code, unreliable on small local models, harder to test, and unnecessary when deterministic orchestration plus single-shot judgment achieves the same outcome.
- **Encrypting the API key at rest** — rejected for this threat model, per decision 3.

## Consequences

- The application gains an outbound LLM client and a scheduled job that consumes tokens or local compute; both are owner-configurable and disabled until a provider is set.
- Judgment quality now varies with the configured model; the librarian's conservative bias (when unsure, leave the entry alone) becomes more important, not less.
- The database and its backups may contain a provider API key.
- Two runtimes must stay behaviourally consistent: both act only through the existing domain functions, so guardrails, the atomic-cap rules, and supersede-never-erase hold identically.
