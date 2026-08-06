# 0005 — Knowledge Layer Lives in Postgres, Not Git

- **Status:** accepted (amends ADR-0002)
- **Date:** 2026-08-06

## Context and Problem Statement

ADR-0002 specified the curated knowledge layer as "markdown + frontmatter versioned in a git repo for human auditability," with Postgres for the journal. The implementation (phases 2-5) stores all layers — journal, library, doctrine, mirrored documents — in Postgres, with immutability enforced by the application (supersede-never-erase, immutable version rows, append-only records). The divergence was surfaced to the owner during phase 7 planning and ratified rather than reworked.

## Decision

Postgres is the single store for all Brain data. The auditability goal of ADR-0002's git layer is met by construction instead: entries are immutable rows; corrections are supersessions preserving full lineage; doctrine and mirrored documents are versioned immutable rows; the adversarial review record of each phase verified these properties empirically. Backups are nightly pg_dump plus a git bundle of the code repository (which contains the specs, ADRs, and all operational docs).

## Alternatives Considered

- Retrofitting a parallel git-backed store for the library — rejected: adds a synchronization surface with real failure modes (drift, partial writes) for no functional gain; git history would duplicate what supersession chains already record.
- Reworking storage to git-as-canonical per the original ADR — rejected: incompatible with atomic multi-compartment deposits, FTS-based search and duplicate hints, and the flags inbox, all of which are load-bearing contract features.

## Consequences

- History inspection uses the Brain's UI/API (supersession chains, version lists) rather than `git log`. This is the one genuine loss versus the original design, judged cosmetic.
- The database volume is the Brain's single source of truth; the backup discipline (ADR-0003 §11, implemented phase 7) is correspondingly load-bearing.
- ADR-0002's storage-layer description is superseded on this point; its API, packaging, and big-data-contract decisions stand unchanged.
