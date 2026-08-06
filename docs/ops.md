# Operations — running Brainard

The operator's manual: fresh deploy, day-to-day admin, backups, migration,
and the one failure mode that has no clean fix (owner-token loss). For
developer/test workflow (running the test suite, tearing down for
development) see `docs/dev.md`; for what to paste into a new AI session see
`docs/onboarding.md`.

## Fresh deploy

The stack is two services (`db`, `api`) plus a named volume (`db_data`)
holding all Postgres data — see `docker-compose.yml`. `docker compose down`
stops containers and **keeps** `db_data` (data persists); `docker compose
down -v` also **removes** it (destroys everything — machines, projects,
library, doctrine, all of it). Only ever run `down -v` when you deliberately
want a wiped-clean brain.

```
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD to a strong value, then mirror it into
# DATABASE_URL and TEST_DATABASE_URL (all three must agree on
# user/password/host/port — see the comments in .env.example)

docker compose up -d --build
```

### Owner-token capture ceremony

On first boot only (no `owner_token` row exists yet), the API prints the
**owner token** once, in the container logs, inside a banner:

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

**Save it immediately**, before doing anything else — it is stored only as a
SHA-256 hash (`app/security.py`) and the plaintext is gone the moment this
banner scrolls off. There is no "forgot my token" recovery endpoint, by
design (contracts-v1.md §1). Recommended: write it straight to a file
outside the repo, readable only by you:

```
docker compose logs --no-log-prefix api 2>&1 | grep -A2 'OWNER TOKEN' | tail -1 | xargs > ~/.brain-owner-token
chmod 600 ~/.brain-owner-token
```

(`--no-log-prefix` matters: without it, `docker compose logs` prepends
`api-1  | ` to every line, which `xargs` would fold straight into the saved
file alongside the token, corrupting it. With the flag, the captured line is
the bare token and nothing else.)

(Or copy it by hand from the terminal — either way, get it off the screen
and into permanent storage before the terminal scrolls or the session ends.)

### `UI_SESSION_SECRET`

Left unset, the UI session cookie is signed with a secret generated fresh
each process start (`app/config.py`) — fine for a LAN single-owner
deployment (a restart just logs everyone out; log back in with the owner
token), but any restart invalidates every open UI session. Set it once, up
front, if that's undesirable:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
# put the result in .env as UI_SESSION_SECRET=...
docker compose up -d api   # restart to pick it up
```

## UI login

Visit `http://<host>:<API_PORT>/` (default port `8300`, see `API_PORT` in
`.env`) — it redirects to `/ui/login`. Paste the owner token into the login
form; there is no separate UI password, the owner token *is* the
credential. A successful login sets a signed session cookie (see
`UI_SESSION_SECRET` above); log out via the "Log out" button in the top bar.

## Minting machines

Every AI session needs a per-machine bearer token (contracts-v1.md §1: one
token per machine, not per session). Two equivalent ways to mint one — both
call the exact same underlying logic (`app/machines.py`), so neither is more
"official" than the other:

**UI** — log in, go to Admin (`/ui/admin/machines`), fill in a name (e.g.
"NUC — Proxmox container 111"), submit. The token — and a ready-to-copy
onboarding paste-line with that token already filled in (see
`docs/onboarding.md`) — are shown **exactly once**, on that response only.
Copy both before navigating away.

**API**:

```
curl -s -X POST http://<host>:<API_PORT>/v1/machines \
  -H "Authorization: Bearer <OWNER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name": "NUC — Proxmox container 111"}'
```

Response includes `token` in plaintext, once. Revoke a machine (UI button,
or `POST /v1/machines/{id}/revoke` with the owner token) if it's
compromised or decommissioned — revocation is immediate and irreversible
(no un-revoke; mint a fresh machine instead).

## Backups

`scripts/backup.sh` (run on the **Docker host**, not inside a container —
it drives the stack via `docker compose exec`):

1. `pg_dump` (custom format, via `docker compose exec -T db pg_dump ...`) into
   `backups/brain-<timestamp>.dump` (gitignored — never committed).
2. Prunes local dumps to the last 14.
3. **If** `BACKUP_TARGET_HOST`, `BACKUP_TARGET_USER`, `BACKUP_TARGET_PATH`
   are all set (env or `.env`): also builds `git bundle create ... --all`
   and `rsync`s the dump + bundle to that host over SSH, then prunes local
   bundles to the last 14 too.
4. **If unset**: logs that the remote push was skipped and stops there —
   "placeholder mode". This is the current state: no second machine exists
   yet (`docs/vision.md`, "Open build-time inputs: backup target machine").
   The script is safe to wire into cron today; it starts pushing off-box the
   moment the three vars are filled in, no code change required.

### Filling in the placeholder vars

Once a backup machine exists, in `.env`:

```
BACKUP_TARGET_HOST=backup-host.lan
BACKUP_TARGET_USER=brain-backup
BACKUP_TARGET_PATH=/srv/brain-backups
```

Requirements on the target: that user must accept the Docker host's SSH key
(passwordless, e.g. via `ssh-copy-id`) and be able to `mkdir -p` the target
path (the script does this itself on each run).

### Cron (host-side — never inside the container)

No cron runs inside either container; the backup script needs `docker
compose` and SSH access to the Docker host itself, neither of which belong
inside the API image. Install on the Docker host's crontab (`crontab -e`):

```
0 3 * * * cd /root/brainard && ./scripts/backup.sh >> /var/log/brain-backup.log 2>&1
```

(Nightly at 03:00; adjust the path if the repo lives elsewhere, and the log
path to wherever you keep host logs.)

Cron runs jobs with a minimal `PATH` that may not include `docker`'s install
directory (commonly `/usr/bin` or `/usr/local/bin`, but distro-dependent) —
if the job silently fails, `command not found: docker` in the log file is
the usual symptom; fix by prefixing the crontab line with an explicit
`PATH=...` or by calling `docker` via its full path.

### Restore procedure (tested)

Verify a dump is structurally sound without touching any real data:

```
docker compose exec -T db pg_restore --list < backups/brain-<timestamp>.dump | head
```

Restore into a **fresh** database (never restore over a live one in place —
`pg_restore` with a plain `-d brain` against an already-populated database
will collide on every object). The tested procedure, restoring into a
scratch database inside the same running `db` service:

```
docker compose exec -T db createdb -U "$POSTGRES_USER" brain_restore_check
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d brain_restore_check \
  < backups/brain-<timestamp>.dump
# spot-check, e.g.:
docker compose exec -T db psql -U "$POSTGRES_USER" -d brain_restore_check \
  -c 'select count(*) from machines;'
docker compose exec -T db dropdb -U "$POSTGRES_USER" brain_restore_check
```

For a **real disaster recovery** (the `db_data` volume is gone or corrupt):
bring up a fresh stack (`docker compose up -d db`, no `api` yet so nothing
writes to it prematurely), then:

```
docker compose exec -T db dropdb -U "$POSTGRES_USER" brain
docker compose exec -T db createdb -U "$POSTGRES_USER" brain
cat backups/brain-<timestamp>.dump | docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d brain
docker compose up -d api
```

`alembic upgrade head` (already wired into the `api` container's `CMD`, see
`Dockerfile`) is a no-op against a dump taken from an up-to-date schema, but
runs harmlessly either way on container start.

## Migration to a new host

Two options, in order of preference:

**1. Copy the volume (preferred — exact, includes everything, fastest):**

```
# old host
docker compose down            # stop containers, keep db_data
docker run --rm -v brainard_db_data:/from -v /tmp:/to alpine \
  tar czf /to/db_data.tgz -C /from .
scp /tmp/db_data.tgz newhost:/tmp/

# new host (repo already cloned, .env already configured with the SAME
# POSTGRES_USER/PASSWORD/DB as the old host -- Postgres's on-disk format
# doesn't care, but the API's DATABASE_URL must still match)
docker compose up -d db        # creates the empty named volume
docker compose down
docker run --rm -v brainard_db_data:/to -v /tmp:/from alpine \
  sh -c "rm -rf /to/* && tar xzf /from/db_data.tgz -C /to"
docker compose up -d --build
```

This is exactly ADR-0002's portability decision ("migrate by copying
volumes") — no export/import step, no downtime beyond the copy itself, and
every table (including `owner_token` and `machines`) comes across intact —
**existing owner and machine tokens keep working** on the new host.

**2. Export + fresh install (slower, tokens do NOT carry over):**

Use `GET /v1/export` (owner token) to get a full NDJSON dump of every table
except `owner_token` (deliberately excluded — see app/routers/export.py;
the new host gets its own fresh owner token at first boot regardless),
then stand up a brand-new stack on the new host (fresh `docker compose up`,
fresh owner token, fresh machine tokens) and re-import the data by whatever
means suits the new host (there is no built-in `/v1/import` — this path is
for cases where volume copy isn't possible, e.g. moving to a host that
can't reach the old one directly). Prefer option 1 whenever both hosts can
exchange files at all.

## Owner-token loss

Documented honestly, because there is no good answer:

- The owner token is stored **only as a SHA-256 hash** (`owner_token.token_hash`,
  `app/security.py`). There is no reset-my-token flow and none is planned —
  a recoverable owner credential would be a standing security weakness in a
  root credential that gates machine management, doctrine writes, proposal
  approvals, and export.
- **If lost, the honest options are:**
  1. **Wipe and start fresh** — `docker compose down -v && docker compose up -d --build`
     mints a brand-new owner token (printed once, same ceremony as first
     boot) but **destroys every machine, project, doctrine version, and
     library entry** — a fresh brain, not a recovered one.
  2. **Manual database surgery** — connect directly
     (`docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"`),
     generate a new token client-side, hash it the same way the app does
     (`sha256`, hex digest — see `app/security.py::hash_token`), and
     `UPDATE owner_token SET token_hash = '<hex>' WHERE id = 'singleton'`.
     This keeps every machine, project, and library entry intact — only the
     owner credential changes. Example:
     ```
     python3 -c "import secrets, hashlib; t = 'brnown_' + secrets.token_urlsafe(32); print('token:', t); print('hash:', hashlib.sha256(t.encode()).hexdigest())"
     docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
       -c "UPDATE owner_token SET token_hash = '<hash from above>' WHERE id = 'singleton';"
     ```
     Save the printed `token:` value immediately — same one-shot rule as
     first boot.
- **Recommended practice**: capture and save the owner token the moment it
  prints at first boot (see the capture ceremony above), in at least one
  place outside the repo (a password manager, a root-only file with `chmod
  600`). Losing it is fully avoidable; recovering from losing it is not.
