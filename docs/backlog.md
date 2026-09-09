# Backlog — known gaps, not yet scheduled

Items observed in the field (usually via a Builder/Commander session hitting
a limit and filing a lesson) that are real gaps but not urgent enough, or
not yet owned enough, to act on immediately. This is not a doctrine
document — nothing here is a rule; it is a punch list the owner triages
when there's room for it. Compare `docs/vision.md`'s "Open build-time
inputs" (deploy-time config still to be filled in) — this file is the
general-purpose equivalent for everything else.

Each entry: what the gap is, the evidence it's real, and why it matters.
Entries are removed (or moved to an ADR / a shipped change) once acted on,
not left to rot as stale checkboxes.

## 1. No endpoint to fetch a mirrored document's content back out of the Brain

Sessions can mirror ADRs/project docs into the Brain (contracts-v1.md §5,
"ADRs & project docs — mirror model") and search them (`GET /v1/search`,
`GET /v1/library/{id}` for the search index), but there is **no endpoint
that returns a mirrored document's actual content**. `GET
/v1/documents/{id}` doesn't exist; `?path=` lookups 404. `GET
/v1/library/{id}` only serves library entries (lessons/howto/reference), not
mirrored documents. A session that deposited a decision's content earlier
has no way to retrieve that text back from the Brain later — only its
existence, path, version, and a search snippet.

**Evidence:** lesson filed from project `7rf.ae` (2026-09-08), library entry
`01M215CA0XAQ9AX9KPRYBJ47TZ` / event `01M20B7TMBP0088DGC94B5DWND`: "Brain API
gap observed: mirrored documents (`documents[]` deposits) are searchable
(snippets, path, version) but there is no endpoint to fetch a mirrored
document's content back (`/v1/documents/{id}` and `?path=` return 404; only
`/v1/library/{id}` exists for library entries). Consequence: keep
local/repo copies of anything you may need to re-read; the Brain mirror is
for search and history, not retrieval." A second, independent entry
(`01M20B7TMBP0088DGC94B5DWNE`) reached the same conclusion: "Brain mirrored
documents cannot be fetched back by API -- keep a local copy."

**Why it matters:** the mirror model exists so "every decision ever made
[is] searchable fleet-wide" (contracts-v1.md §5) — but search-without-retrieval
means an agent can find *that* a decision exists and roughly what it says
(a snippet), yet cannot pull its full text if the source repo isn't at hand
(a different machine, a repo not checked out, a decision from a project the
session isn't actively working in). That undercuts the "never solve twice"
promise for anything that isn't a library entry.

**Not a doctrine matter.** This is a missing API feature (a
`GET /v1/documents/{id}` route, or content-inclusion on the existing
`/v1/library/{id}`-style read path for documents), not a rule anyone is
breaking or a policy to write down.

## 2. Builder agents are blocked by the Claude Code permission classifier from system installs and writes outside their working directory

Observed workaround: run the install from the **Commander's** session
instead of the Builder's. That works but is a manual, ad hoc escape hatch
every time it recurs, not a fix — and it means privileged steps end up
running in a session with broader trust than the task strictly needs.

**Evidence:** lesson + event filed from project `7rf.ae` (2026-09-08),
library entry `01M20S4A7TAZT7T7K1G3FNKHV3` / `01M215C7ZCRZHHEJGN83NKM5JX` /
event `01M20S4A7TAZT7T7K1G3FNKHV2`: "Builder sessions under the permission
classifier cannot perform system installs or write outside their working
directory (tar to `/opt`, symlinks in `/usr/local/bin`, `apt` were all
refused). Worked around by running the installs from the Commander's
session via a Sonnet worker. Will recur at S7 (systemd units, nginx site,
`/srv`, `/var/lib/7rf`) unless the owner grants the Builder session a
permission rule -- raised with the owner."

**Why it matters:** it already recurred once and is flagged to recur again
at a known future milestone (S7: systemd units, nginx site, `/srv`,
`/var/lib/7rf`) on the same project. Left unaddressed, every future Builder
task that needs a system-level install repeats the same manual detour.

**Not a doctrine matter.** This is a Claude Code tooling/permissions
decision for the owner to make (e.g. a scoped permission rule for Builder
sessions, or a deliberate policy that privileged installs always route
through Commander) — not something doctrine text in the Brain can fix.
