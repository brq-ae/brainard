# Brainard

One central hub, hosted on this container, that every AI session the owner runs — any tool, any machine — plugs into. It does three jobs: **teaches the rules**, **carries project context across sessions**, and **grows a library so nothing is ever solved twice**.

## What it holds

- **Doctrine** — the rulebook (never assume, never guess, git everything, ADR decisions), global plus optional per-project overlays
- **Lessons & gotchas** — the never-solve-twice content
- **Runbooks & how-tos** — procedures for infrastructure and tools
- **Projects** — registry, docs, ADRs, and **handoff notes** (the continuity mechanism)
- **Journal** — full activity stream of everything sessions did, delivered in checkpoints

## The operating loop

1. **Bootstrap:** one pasted line into any new session → it fetches doctrine + the project's latest handoff note, and starts disciplined and oriented.
2. **Work:** full activity accumulates locally — the Brain is a checkpoint system, not live telemetry.
3. **Deposit:** daily when there is new material, and always at session end (handoff note + activity + lessons). If the hub is unreachable: queue and retry, never block work.
4. **Recall:** fresh read at every session start; re-read on the owner's command ("remind yourself"); at a block, the session asks the owner's permission to check the Brain before searching the internet.
5. **Correct:** supersede, never erase — full history forever.
6. **Curate:** a fully autonomous librarian on a schedule — dedups, merges, organizes, harvests lesson candidates. Safe because its every action is a supersession, hence reversible.

## Trust & access

All writes are trusted at full weight — zero friction. HTTP API front door (usable by any AI tool on any machine), per-machine bearer tokens so provenance is honest and any machine is individually revocable. Private LAN today; VPN/TLS-ready for later external access.

## Infrastructure

FastAPI + Postgres in a Docker Compose stack (verified working on this container), fully portable — migrate by copying volumes. Backups: the Brain pushes nightly pg_dump + git mirror to a second machine on the LAN. UI: read-only browser view — search the library, read handoffs, skim activity.

## Open build-time inputs

- Backup target machine (address + SSH access)
- Runtime that powers the librarian (which AI executes it, on what schedule)
