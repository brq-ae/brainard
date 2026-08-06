# Brainard — Contracts v1

Settled with the owner in a five-item walkthrough on 2026-08-06. Key rulings recorded in ADR-0004. Architecture in ADR-0002; operating model in ADR-0003.

## Principles (apply everywhere)

- **Checkpoints, not telemetry:** sessions accumulate locally, deposit in atomic batches.
- **Never lose knowledge at deposit time:** validation is strict but every rejection is self-explaining and has a scripted recovery; fuzzy judgments (duplicates, forks) never block acceptance.
- **Supersede, never erase.** History is permanent everywhere.
- **Append-only vocabularies:** event kinds and statuses are never renamed or removed, only added.
- **IDs are ULIDs.** Lists paginate by cursor. Readers see `active` content by default; history on request.

## 1. Identity & authentication

- **Per-machine bearer tokens.** One token per machine (not per session); all sessions on a machine share it. The server binds every request to its machine record — provenance is structural, not self-reported.
- Machine record: `id`, `name` (owner's free-form label, e.g. "NUC — Proxmox container 111"), `created`, `last_seen` (updated on any authenticated call), `status` (`active`/`revoked`). Tokens are stored **hashed**; shown in full exactly once, at creation.
- **Owner token:** a single root credential created at install, shown once. Gates: machine management, doctrine writes, proposal approvals, export.
- Sessions self-report a `session` id inside deposits (trusted, per the full-trust stance).

## 2. The checkpoint deposit — `POST /v1/deposits`

One atomic batch: fully accepted or fully rejected.

**Envelope:** `deposit_id` (client ULID — idempotency key; retries with the same id are never duplicated), `tool`, `session`, `project` (unknown names auto-create a registry stub), `reason` (`session_end` | `daily` | `manual`), `client_ts`, optional `doctrine_version` (the version the session bootstrapped with). Server sets `received_at` and `machine` (from token).

**Compartments:**
- `events[]` — activity since last deposit. Each: `seq`, `ts`, `kind`, `summary` (one line), optional `payload` (JSON, capped 256 KB), optional `tags[]`.
- `knowledge[]` — library entries (schema §3), including entries flagged as doctrine proposals and mirrored ADRs/docs (§5).
- `handoff` — structured note: *where the project stands / in flight / blocked / next steps* (+ free notes). **Rule: a `session_end` deposit must contain either `handoff` or `no_handoff: "<reason>"`. Silence is rejected.** Waivers are recorded and visible; contradictions (waiver despite heavy activity) are librarian flags.
- `metrics` — fully optional, any subset valid: `model`, `tokens_in`, `tokens_out`, `cost_estimate`, `duration`. Absence is never a violation.

**Event kinds — fixed vocabulary, strictly enforced:**

| Kind | Meaning |
|---|---|
| `session.started` / `session.ended` | Session lifecycle |
| `work.started` / `work.completed` | A work item began / finished |
| `artifact.produced` | Commit, file, build, deploy produced |
| `decision.made` | Decision worth remembering (may reference an ADR) |
| `error.hit` | Something broke |
| `lesson.candidate` | Worth learning from, not yet written up — librarian harvest queue |
| `note` | Everything else, refined by tags |

Unknown kinds reject the deposit. **Scripted recovery (part of doctrine):** the rejection response lists exactly which events failed and why; the client relabels unknown kinds to `note` (original kind preserved as a tag) and resends. Vocabulary is append-only; rollout order for new kinds is hub first, doctrine second. Recurring `note` tags are the librarian's promotion signal. One happening with two natures (e.g., an error that is also a lesson) = two events sharing a tag.

## 3. Library entries

Markdown body + frontmatter:

| Field | Set by | Notes |
|---|---|---|
| `id` | server | ULID |
| `title` | writer | |
| `namespace` | writer | `lessons` \| `howto` \| `reference` — exactly three shelves. Incidents = `lessons` + `incident`/severity tags. New shelf test: "does an AI search it differently?" — otherwise it's a tag |
| `project` | writer, optional | Absent = universal knowledge |
| `tags[]` | writer | |
| `status` | server | `active` \| `superseded` (auto-set when another entry names this one) \| `retired` (explicit close, requires a reason; for wrong/obsolete knowledge with no replacement) |
| `source` | mixed | `machine` from token; `tool`, `session` from writer |
| `created` | server | |
| `supersedes[]` | writer, optional | **Array** — merges have one child, many parents. Any session may supersede any entry (full trust) |
| body | writer | Free markdown. Server checks only: non-empty, under size cap |

- **Forks** (two entries superseding the same parent) are accepted, left active as siblings, and flagged for the librarian.
- **Duplicates:** the server never blocks on similarity. On arrival it runs a cheap full-text similarity check and attaches "possibly duplicates entry X" hints — visible to readers, and the librarian's pre-built dedup queue.
- **Templates** (lesson: Situation/Problem/Fix/Why it works; howto: numbered steps + verify block) are doctrine guidance served at bootstrap — never server-validated. The librarian may reshape entries into house style via supersession.

## 4. Doctrine

- **Owner-only writes.** The one collection where full trust does not apply — no AI may alter the rulebook.
- **AI proposals:** sessions file doctrine proposals as flagged library entries (inert — never served at bootstrap) with rationale and evidence. The owner approves via the admin area; approval promotes the change into doctrine.
- **Two-tier rules:** every global rule has a stable ID (`G1`, `G2`, …) and a tier: **non-negotiable** (immutable everywhere; includes never-assume, never-guess, never-execute-without-permission) or **default** (project overlays may override by naming the ID). The server rejects overlays that touch non-negotiables — at write time.
- **Layering:** one global rulebook + optional per-project overlays; the server compiles them (winners only) for bootstrap.
- **Versioned:** every doctrine change bumps a version; deposits may stamp the version the session ran under.

## 5. Projects

Thin registry spine: `name` (unique key; auto-stubbed on first mention by any deposit or bootstrap), `description`, `status` (`active`/`paused`/`done`) — both writable by sessions or owner; `machines` (server-derived from deposits); optional doctrine overlay (owner-only).

Attached to the spine:
- **Handoff chain:** every handoff note, time-ordered, immutable. Temporal snapshots — no supersession; latest-by-time wins; bootstrap serves the latest; history queryable.
- **Journal slice** and tagged library entries.
- **ADRs & project docs — mirror model:** the project's own git repo stays canonical (repos remain self-contained). When a session writes or updates an ADR/doc, its next deposit carries a copy; the Brain stores it searchable under the project. Doctrine mandates: ADR written → deposit carries it. This makes every decision ever made searchable fleet-wide.

## 6. Bootstrap — `GET /v1/bootstrap?project=X`

The one-paste line a session receives: hub URL + machine token + project name → fetch and obey.

Response — five components, **markdown canonical** (`?format=json` variant available), under a **hard size budget** (bounded overlay, one handoff note, capped digest):
1. **Compiled doctrine** — global + overlay resolved, non-negotiables marked.
2. **Project context** — registry facts + the latest handoff note.
3. **Operating instructions** — deposit endpoint and triggers (daily + session end), event kinds, handoff-or-waiver rule, queue-on-unreachable, how to search, how to file proposals. The API is self-teaching.
4. **Templates** — handoff, lesson, howto.
5. **Lessons digest** — titles + one-liners of active project-tagged library entries (capped ~20), so sessions know what exists and can ask to read it.

Unknown project → auto-stub + global doctrine + orientation. Every fetch is logged server-side (machine, project, doctrine version, timestamp).

## 7. API surface

**Session-facing (machine token):** `GET /v1/bootstrap` · `POST /v1/deposits` · `GET /v1/search?q=&scope=` · `GET /v1/library/{id}` (entry + chain + duplicate hints) · `GET /v1/projects/{name}` · `GET /v1/projects/{name}/handoffs`

**Owner-facing (owner token):** `POST /v1/machines` · `GET /v1/machines` · `POST /v1/machines/{id}/revoke` · `POST /v1/doctrine/global` · `POST /v1/doctrine/overlays/{project}` · `GET /v1/doctrine` · `GET /v1/proposals` · `POST /v1/proposals/{id}/approve` · `GET /v1/export` (bulk NDJSON) · `/` (UI: read-only browse + owner-gated admin)

**Ops:** `GET /healthz` (unauthenticated).

**Search default scope:** library + mirrored decisions + handoffs. The journal is opt-in per query — recall searches the books, not the security footage.
