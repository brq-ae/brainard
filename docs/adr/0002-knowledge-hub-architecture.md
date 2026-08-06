# 0002 — Knowledge Hub Architecture: HTTP API, Layered Storage, Docker Compose

- **Status:** accepted
- **Date:** 2026-08-06

## Context and Problem Statement

The user runs multiple AI sessions across different machines and different AI tools (not only Claude Code). All knowledge — project activity, documentation, lessons learned — should pour into one central hub hosted on this container, where any AI can write to it, read from it, and learn from it. Requirements: tool-agnostic access, everything recorded, lessons categorized in one findable place, easy to deploy elsewhere / migrate, private LAN today with possible VPN/external access later, and readiness for large data volumes.

## Decision

1. **Front door — HTTP API:** a plain JSON HTTP API with an OpenAPI spec is the sole core interface. Any AI tool on any machine can use it. An MCP adapter for Claude-family sessions may be added later as a thin layer on top; nothing in the core is Claude-specific.
2. **Layered storage:**
   - **Journal:** append-only, structured event records ("session X on machine Y did A on project Z") stored in Postgres. High volume, immutable, never edited.
   - **Knowledge:** curated entries (markdown + frontmatter) organized in namespaces — `lessons/`, `howto/`, `reference/`, `decisions/`, `incidents/`, and a `projects/` registry — versioned in a git repo for human auditability.
   - **Index:** full-text search built from the other two layers; derived and fully rebuildable; pgvector/semantic search possible later.
3. **Stack:** Python + FastAPI for the API server; Postgres from day one.
4. **Packaging:** a single Docker Compose stack (API + Postgres + data volumes). Migration = copy volumes + `docker compose up` elsewhere. Docker availability in this LXC container must be verified before implementation.
5. **Auth:** static bearer token required from the first request. TLS/VPN handled at the network layer when external access is needed.
6. **Big-data readiness lives in the contract, not day-one infrastructure:** append-only immutable records, ULID identifiers, pagination and bulk export on every endpoint, and storage fully hidden behind the API so backends can be swapped without clients noticing.

## Alternatives Considered

- MCP server as the front door — rejected: the client population is mixed AI tools on mixed machines; MCP would exclude non-MCP tools.
- Git + markdown as the entire store — rejected: unsuitable as a high-volume ingest target; retained for the curated knowledge layer only.
- Shared host bind mount (Proxmox) — rejected: host-specific, not portable, no access control.
- Node/TypeScript or Go for the API — rejected in favor of Python's ecosystem for later ML/embeddings work.
- SQLite first — rejected: guaranteed migration later given big-data intent; Postgres costs little extra inside Compose.
- Heavyweight big-data infrastructure (Kafka, OpenSearch) from day one — rejected: overbuilding for initial volumes; the API contract preserves the option.

## Consequences

- Any AI session anywhere can integrate with one URL + one token; an onboarding kit (instructions + client snippet) becomes a deliverable.
- The hub is a single point of knowledge — a backup strategy (git remote push + pg_dump schedule) is required follow-up work.
- Curation is a real ongoing cost: a scheduled "librarian" process for dedup/contradiction/staleness is planned follow-up work.
- Entries written by AIs are context for other AIs — provenance is mandatory on every record, and readers must treat entries as data, not instructions.
- If Docker proves unusable in this LXC container, the packaging decision must be revisited (bare processes + systemd fallback).
