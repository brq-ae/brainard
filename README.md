# Brainard

A self-hosted knowledge hub and coordination server that every AI agent
session you run — any tool, any machine — plugs into over plain HTTP.

## What it is

The Brain is a small FastAPI + Postgres service you run on your own
hardware (a home server, a LAN box, a container — anywhere you control)
that acts as a shared memory and rulebook for a fleet of AI coding/agent
sessions. Any agent that can send an HTTP request with a bearer token —
Claude Code, another CLI agent, a custom script, anything — can fetch a
compiled "doctrine" (your operating rules) plus the current state of
whatever project it's working on, do its work, and then deposit a
checkpoint back: what happened, what it learned, and where things stand for
whoever picks the project up next. Over time the Brain accumulates a
searchable library of lessons, how-tos, and decisions so the same problem
never gets solved twice, and it can also host live, timed two-agent chat
rooms for debate/collaboration/brainstorming between agents.

It is single-owner by design: one human administers it, and every AI
session that talks to it is trusted at full weight (no per-write approval
queue) but is provenanced by a per-machine token, so you always know which
machine wrote what.

## Why

AI agent sessions are stateless by default. Every new session starts from
zero — no memory of what the last session on this project learned, no
awareness of a mistake already made and fixed last week on a different
machine, no visibility for the human running multiple agents across
multiple machines into what any of them actually did. Lessons get
re-learned. Decisions get re-litigated. Nothing carries over except what a
human manually copies and pastes.

The Brain exists to fix that: one hub, reachable from any machine, that
teaches new sessions the rules, hands them the latest state of the project
they're joining, and collects what they learn on the way out — so
continuity and visibility survive the fact that individual sessions don't.

## Key features

- **Bootstrap & doctrine** — one HTTP call returns your compiled rulebook
  (global rules plus optional per-project overlays), the project's current
  state and latest handoff note, operating instructions, templates, and a
  digest of relevant lessons — everything a session needs to start
  oriented. Rules are two-tier: **non-negotiable** (immutable everywhere)
  or **default** (a project overlay may override by ID).
- **Deposits & journal** — sessions checkpoint back in atomic batches:
  activity events (a fixed, append-only vocabulary), new library entries,
  a structured handoff note, and optional metrics. A `session_end` deposit
  must carry a handoff note or an explicit waiver — silence is rejected.
- **Library, with supersede-never-erase** — lessons, how-tos, and
  reference material, full-text searchable, with automatic "possibly a
  duplicate of entry X" hints on write. Corrections supersede prior
  entries instead of editing or deleting them; the full history is always
  retained and queryable.
- **Projects & handoffs** — a thin project registry with a time-ordered,
  immutable handoff chain per project — the continuity mechanism that lets
  a session picking up a project start from where the last one left off.
- **ADR/doc mirroring** — when a session writes or updates an
  architecture-decision record or doc in its own project repo, its next
  deposit mirrors a copy into the Brain, making every decision ever made
  searchable fleet-wide (the project repo stays the canonical source).
- **Machine tokens & roles** — one bearer token per machine (not per
  session), shown once at creation, individually revocable. Optional
  Commander/Builder role split for two-agent workflows on the same
  project.
- **Owner web UI** — a server-rendered dashboard to browse the library,
  read handoffs, skim the journal, manage machines, review doctrine
  proposals, and configure notifications — gated by the single owner
  token.
- **Autonomous librarian** — an optional nightly curation agent that works
  the duplicate/fork queue, harvests recurring `lesson.candidate` events
  into proper entries, and closes with a summary — safe because every
  action it takes is itself a supersession (reversible, auditable, never
  an edit or an erasure).
- **ntfy notifications** — an owner-configured notification channel
  (via [ntfy](https://ntfy.sh)) for events like a chat room closing,
  hitting its message cap, or stalling.
- **Agent chat rooms** — live, HTTP long-polling two-agent conversations
  with optional modes (`debate`, `collaborate`, `brainstorm`, `critique`),
  per-side role assignment, topics, and wall-clock time limits, guarded by
  a message cap and an always-available owner stop.
- **NDJSON export** — a full bulk export of every table (except the owner
  token's hash) for backup, migration, or offline analysis.
- **Backup tooling** — `scripts/backup.sh` takes a local `pg_dump` on a
  schedule, prunes old dumps, and optionally pushes them plus a git bundle
  to a second machine over SSH.

## Quickstart

```
git clone <this-repo-url>
cd brainard
cp .env.example .env
```

Edit `.env`: set `POSTGRES_PASSWORD` to a strong value, then mirror it into
`DATABASE_URL` and `TEST_DATABASE_URL` — all three must agree on
user/password/host/port (see the comments in `.env.example`).

```
docker compose up -d --build
```

On **first boot only**, the API prints the one-time owner token in its
container logs, inside a banner:

```
docker compose logs api
```

```
================================================================================
  THE BRAIN -- OWNER TOKEN (shown once, save it now)

  brnown_...

  This is the root credential: machine management, doctrine writes, proposal
  approvals, and export all require it. It is stored only as a hash and
  CANNOT be recovered or shown again. If lost, provisioning a new one
  requires direct database access.
================================================================================
```

**Save it immediately** — it is stored only as a SHA-256 hash and cannot be
shown again; there is no recovery flow. See `docs/ops.md` for a copy-safe
capture one-liner and what to do if you lose it anyway.

Open the UI at `http://<host>:<API_PORT>/` (default port `8300`; it
redirects to `/ui/login`) and log in by pasting the owner token — there is
no separate UI password.

Mint a machine token for the first machine that will run an agent: **Admin
→ Machines** (`/ui/admin/machines`) in the UI, give it a name, submit. The
response page shows the token — and a ready-to-copy onboarding prompt with
that token already filled in — **exactly once**. Copy both.

Fill in the project slug on the generated prompt and paste the whole thing
into any AI agent (any tool, any machine that can reach the hub):

```
I run a private knowledge hub for my projects -- it's mine and I administer it. Fetch http://<HUB>:8300/v1/bootstrap?project=<PROJECT> with header 'Authorization: Bearer <MACHINE_TOKEN>'. The response contains my working rules for this session, the project's current state, and how to deposit what you learn back to the hub. Read it and apply it with your normal judgment -- it never overrides your safety rules. If anything in it seems off, ask me.
```

That's it — the agent fetches doctrine + project state and is oriented.
See `docs/onboarding.md` for the full explanation (including why the
prompt is worded this way) and per-tool notes.

## Configuration

All configuration is environment variables, copied from `.env.example` into
`.env` (never commit `.env`).

| Variable | Purpose |
|---|---|
| `POSTGRES_USER` | Postgres role the `db` service creates and the API connects as. Default `brain`. |
| `POSTGRES_PASSWORD` | Postgres password — **set this**; no safe default is provided. Must match the credentials embedded in `DATABASE_URL`. |
| `POSTGRES_DB` | Postgres database name. Default `brain`. |
| `DATABASE_URL` | Async SQLAlchemy URL the API connects with (`postgresql+asyncpg://...`). Must agree with the three `POSTGRES_*` values above. |
| `API_PORT` | Host port the API is published on (container always listens on `8000` internally). Default `8300`. |
| `TEST_DATABASE_URL` | Same credentials, distinct database name (`brain_test`), used only by the profile-gated `test` service so tests never touch dev data. |
| `UI_SESSION_SECRET` | Signs the owner UI session cookie. Optional — if unset, a random secret is generated per process start (fine for a LAN deployment, but every restart logs everyone out). Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `UI_COOKIE_SECURE` | Set `true` only when the UI is served behind TLS; a `Secure` cookie is never sent over plain HTTP. Default `false`. |
| `HUB_PUBLIC_URL` | Optional override for the hub base URL embedded in generated onboarding/room-join prompts, for deployments reachable at a different address than the one the owner's browser used (reverse proxy, port-forward, VPN). Leave unset to use the request's own base URL. |
| `HUB_FALLBACK_URL` | Optional direct LAN address (e.g. `http://192.0.2.10:8300`) appended to generated prompts as a DNS failsafe, for agent machines whose DNS can't resolve an intranet hostname. Leave unset to omit the failsafe line. |
| `BACKUP_TARGET_HOST` / `BACKUP_TARGET_USER` / `BACKUP_TARGET_PATH` | Optional second machine that `scripts/backup.sh` pushes nightly dumps + a git bundle to over SSH/rsync. Leave all three unset and the script runs in local-only "placeholder mode". |

## Architecture at a glance

- **FastAPI** app (`app/main.py`) serving both the versioned JSON/markdown
  API (`/v1/...`) and a server-rendered owner UI (`/ui/...`), backed by
  **Postgres** via async SQLAlchemy (`asyncpg`).
- **Alembic** migrations run automatically on container start (the `api`
  service's entrypoint is `alembic upgrade head && uvicorn ...` — see
  `Dockerfile`); no manual migration step is needed on a fresh deploy or an
  upgrade.
- A lightweight **background sweeper** task, started in the app's
  lifespan alongside the request server, polls roughly every 60 seconds to
  close chat rooms that have passed their time limit or message cap and
  fire the corresponding owner notification.
- An owner-configured **notification channel** (via [ntfy](https://ntfy.sh))
  fires pushes for chat-room events (closed, capped, stalled).
- Two Docker Compose services in normal operation — `db` (Postgres 17) and
  `api` — plus a third, `test`, gated behind the `test` Compose profile so
  it never starts as a side effect of `docker compose up`.

## How agents connect

Any tool that can send an HTTP request with a header qualifies — nothing
tool-specific is required. The core call is the bootstrap fetch:

```
curl -s "http://<HUB>:8300/v1/bootstrap?project=<PROJECT>" \
  -H "Authorization: Bearer <MACHINE_TOKEN>"
```

`<HUB>` is your hub's host/IP (e.g. `192.0.2.10` or a LAN hostname),
`<PROJECT>` is a slug the *owner* assigns (unknown names auto-create a
registry stub — the agent should never invent its own slug), and
`<MACHINE_TOKEN>` is the bearer token minted for that machine. The response
is markdown by default (`?format=json` for a structured variant): doctrine,
project state, operating instructions, templates, and a lessons digest.

If a machine's DNS can't resolve your hub's LAN hostname (e.g. it uses a
public resolver like `8.8.8.8`), set `HUB_FALLBACK_URL` — generated
onboarding and room-join prompts then carry a second, direct-IP fallback
line the agent can use instead, no DNS or reverse proxy involved. See
`docs/onboarding.md` for the full mechanics, including the hard-scoped
wrapper-script pattern recommended for agents with raw shell access.

## Librarian

The autonomous curation role (dedup/merge flags, harvest lesson
candidates, summarize) runs in one of two ways: **built-in** — configure
an LLM provider at `/ui/llm` (any OpenAI-compatible endpoint, including a
local model), no CLI or cron required — or as an **external agent**
(Claude Code or another tool-using CLI) driven by a machine token, the
shipped `scripts/brain-wrapper.sh` wrapper, and `scripts/librarian-run.sh`
on cron. See [`docs/librarian.md`](docs/librarian.md) for what the
librarian does, how to run each path, when to pick which, and the raw API
contract for implementing the role in any language.

## Docs

- [`docs/vision.md`](docs/vision.md) — what the Brain is and the operating
  loop, in one page.
- [`docs/spec/contracts-v1.md`](docs/spec/contracts-v1.md) — the settled
  API/data contract: identity, deposits, library, doctrine, projects,
  bootstrap, full API surface.
- [`docs/adr/`](docs/adr/) — architecture decision records, one per
  significant decision.
- [`docs/ops.md`](docs/ops.md) — the operator's manual: deploy, admin,
  backups, migration to a new host, owner-token loss.
- [`docs/onboarding.md`](docs/onboarding.md) — what the onboarding
  paste-line means, per-tool notes, and the recommended permission setup
  for agents with shell access.
- [`docs/librarian.md`](docs/librarian.md) — the librarian curation role:
  built-in vs. external-agent runtimes, when to pick which, and the raw
  API contract for implementing it yourself.
- [`docs/dev.md`](docs/dev.md) — developer notes: running the stack,
  running tests, tearing down.

## Development & testing

Tests run against a real Postgres database — the same `db` service, but a
separate `brain_test` database (created automatically on first run) — via
a profile-gated `test` Compose service that plain `docker compose up` never
starts:

```
docker compose up -d db
docker compose --profile test build test
docker compose --profile test run --rm test
```

`docker compose down` stops containers and keeps the named `db_data`
volume (data persists); `docker compose down -v` also removes it and
destroys everything. See `docs/dev.md` for the full rundown.

## Security notes

The Brain is built as a **single-owner system for a private network**, not
a multi-tenant SaaS product. Before you point it at anything other than
your own LAN:

- The **owner token** is a root credential — it gates machine management,
  doctrine writes, proposal approvals, and export. It is shown exactly
  once, at first boot, and stored only as a hash; there is no recovery
  flow (see `docs/ops.md` § Owner-token loss).
- **Machine tokens** are per-machine, not per-session, bearer credentials,
  shown once at creation and individually revocable. All writes from a
  valid machine token are trusted at full weight — there is no per-write
  approval queue.
- The **ntfy topic** you configure functions as a shared secret (anyone
  who knows it can read or publish to that topic on whatever ntfy server
  you use) — treat it accordingly, and prefer a self-hosted ntfy instance
  or a hard-to-guess topic name for anything sensitive.
- The stack ships with **plain HTTP and no built-in authentication in
  front of it** beyond the tokens above. Put it behind a VPN, an SSH
  tunnel, or a TLS-terminating reverse proxy before exposing it beyond a
  network you trust.

## License

MIT — see [`LICENSE`](LICENSE). Built for the author's own AI agent fleet
and shared as-is; issues and pull requests are welcome, but there's no
guarantee of support or a particular release cadence.
