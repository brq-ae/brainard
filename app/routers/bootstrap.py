"""Bootstrap -- GET /v1/bootstrap?project=X (contracts-v1.md §6).

"The one-paste line a session receives: hub URL + machine token + project
name -> fetch and obey." Session-facing (machine token only -- an owner
token is rejected the same way as everywhere else that requires a machine:
`require_machine`'s `machine_token_required`, since bootstrap is what
*sessions* run under, never the owner).

Five sections, markdown canonical, `?format=json` variant, hard size budget,
doctrine compiled from the current global + the project's overlay (if any),
every fetch logged (`bootstrap_fetches`).
"""

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.auth import Principal, require_machine
from app.db import get_db
from app.doctrine import current_global, current_overlay
from app.models import BootstrapFetch, DoctrineVersion, Handoff, KnowledgeEntry, NotificationConfig, Project
from app.notifications import current_config as current_notification_config
from app.routers.deposits import VALID_EVENT_KINDS

router = APIRouter(prefix="/v1/bootstrap", tags=["bootstrap"])

logger = logging.getLogger(__name__)

# Hard cap on the total response (~32 KB, contracts-v1.md §6: "under a hard
# size budget"). Trim order on overflow: lessons digest first, then overlay
# content -- the non-negotiable rules (and everything else in the compiled
# doctrine) are never trimmed.
SIZE_BUDGET_BYTES = 32 * 1024

DIGEST_LIMIT = 20
DIGEST_SNIPPET_CHARS = 120

TEMPLATES: dict[str, str] = {
    "handoff": (
        "```json\n"
        "{\n"
        '  "stands": "<where the project stands right now>",\n'
        '  "in_flight": "<what is actively in progress>",\n'
        '  "blocked": "<what is blocked -- or \\"\\" if nothing is>",\n'
        '  "next_steps": "<what to do next>",\n'
        '  "notes": "<optional free notes>"\n'
        "}\n"
        "```"
    ),
    "lesson": (
        "```markdown\n"
        "## Situation\n"
        "<the context this came up in>\n\n"
        "## Problem\n"
        "<what went wrong, or what was unclear>\n\n"
        "## Fix\n"
        "<what resolved it>\n\n"
        "## Why it works\n"
        "<the underlying reason -- so this generalizes beyond this one incident>\n"
        "```"
    ),
    "howto": (
        "```markdown\n"
        "1. <step one>\n"
        "2. <step two>\n"
        "3. <step three>\n\n"
        "**Verify**: <how to confirm it actually worked>\n"
        "```"
    ),
}


# --- Notifications subsection (rule G9) -- per-event Title prefix / ntfy
# Priority / ntfy Tags. Order matches the task spec's pipe-separated triples
# exactly: input | done | error. Title prefixes are byte-identical to the
# real /usr/local/bin/notify-me script's output (2026-08-16 alignment fix)
# so the raw-curl fallback documented here produces the exact same
# notification a session would get by running the script itself.
_NOTIFICATION_EVENTS: list[dict[str, str]] = [
    {"event": "input", "prefix": "Action Required", "priority": "high", "tags": "question,bell"},
    {"event": "done", "prefix": "Task Finished", "priority": "default", "tags": "checkered_flag,tada"},
    {"event": "error", "prefix": "FAILURE", "priority": "urgent", "tags": "warning,skull"},
]


def _notifications_markdown(config: NotificationConfig | None) -> str:
    """The "Notifications" subsection of operating instructions (rule G9).
    Server-generated so it always reflects the CURRENT channel -- callers
    pass in whatever app.notifications.current_config(db) returns right now,
    never a cached/stale value. Part of the never-trimmed operating
    instructions text (see SIZE_BUDGET_BYTES / `_apply_size_budget` below --
    only the lessons digest and overlay content are ever trimmed).
    """
    lines = [
        "### Notifications",
        "",
        "Rule **G9** keeps the owner informed via notification hooks. The owner wants to know when you "
        "need their input or a decision, and each time you have stopped and are now idle awaiting them.",
        "",
        "**Hooks fire automatically**: `Stop` -> `done` (every turn-end where you go idle in an "
        "interactive session; once, at completion, in a headless run), `Notification` -> `input` (on a "
        "pause that needs the owner). Install and keep these hooks (see the notify-me fleet howto) so the "
        "owner is reliably informed. Rely on them where already installed -- never also fire manually, "
        "that double-alerts; install them where missing, or fire `notify-me` manually at those two "
        "moments until you do.",
        "",
        "**Not on internal sub-steps**: only the stop/idle and input moments above -- never for tool "
        "calls or other activity within a turn.",
        "",
        "**Self-identification**: identify as the agent name the owner assigned for this session, if one "
        "was given. Absent that, fall back to the machine name, then the project name. Never invent a "
        'name, and never identify as "claude" -- that tells the owner nothing about which agent or '
        "session actually needs them.",
        "",
        "**Nothing sensitive**: never put secrets, tokens, credentials, or other private data in a "
        "notification body -- this channel carries none of the Brain's access controls.",
        "",
    ]

    if config is None:
        lines.append(
            "**Current channel**: no notification channel configured yet -- the owner has not posted one "
            "(`POST /v1/notifications-config`). There is nothing to notify to; proceed without this "
            "channel until the owner sets one up."
        )
        return "\n".join(lines)

    lines.append("**Current channel** (from the live config, version " + str(config.version) + "):")
    lines.append("")
    lines.append("Preferred, if the `notify-me` helper script exists on this machine:")
    lines.append("```")
    lines.append('notify-me <event> <agent-name> "<summary>"')
    lines.append("```")
    lines.append("where `<event>` is one of `input`, `done`, `error`.")
    lines.append("")
    lines.append("Raw fallback (use only if `notify-me` is not installed):")
    lines.append("```")
    lines.append(
        'curl -sSf -H "Title: <Event prefix>: <agent-name> (<machine>)" -H "Priority: <high|default|urgent>" '
        '-H "Tags: <question,bell|checkered_flag,tada|warning,skull>" -d "<summary>" '
        f"{config.ntfy_url}/{config.topic}"
    )
    lines.append("```")
    lines.append("with `<Event prefix>`/`<Priority>`/`<Tags>` chosen per event:")
    lines.append("")
    lines.append("| event | Title prefix | Priority | Tags |")
    lines.append("|---|---|---|---|")
    for e in _NOTIFICATION_EVENTS:
        lines.append(f"| `{e['event']}` | {e['prefix']} | `{e['priority']}` | `{e['tags']}` |")
    lines.append("")
    lines.append(
        f"**Pre-approved sync clause**: comparing these values (ntfy URL `{config.ntfy_url}`, topic "
        f"`{config.topic}`) against this machine's local `/usr/local/bin/notify-me` script and updating "
        "THOSE SPECIFIC VALUES there to match is pre-approved maintenance -- no separate confirmation "
        "needed. Mention that you did so in your next handoff. This grant covers nothing else in that "
        "file, and nothing in any other file."
    )
    return "\n".join(lines)


def _operating_instructions_markdown(notification_config: NotificationConfig | None) -> str:
    """Server-generated, written from the actual implemented routes (not
    aspiration) -- event kinds are pulled straight from the deposits
    router's own vocabulary constant, so this section can never drift out of
    sync with what a deposit will actually accept. The full deposit schema
    below (envelope, events[], knowledge[], documents[]) is likewise a
    direct transcription of what app/routers/deposits.py actually enforces
    (DepositRequest + `_validate_knowledge_shape`/`_validate_documents_shape`
    in that module) -- this is the only doctrine a cold client (no prior
    context, no access to the source) has to go on, so every field listed
    here must be verified against the code, not assumed.
    """
    kinds = ", ".join(f"`{k}`" for k in sorted(VALID_EVENT_KINDS))
    return (
        "Deposit checkpoints via `POST /v1/deposits` (machine token, atomic -- fully accepted or fully "
        'rejected). Two triggers matter most: `reason: "daily"` (periodic checkpoint) and '
        '`reason: "session_end"` (end of a session); `reason: "manual"` is also accepted for ad hoc '
        "deposits. `deposit_id` is a client-supplied ULID and the idempotency key -- retries with the same "
        "id are never duplicated.\n\n"
        "**Envelope fields** (top level of the deposit body):\n"
        "- Required: `deposit_id` (ULID string, idempotency key), `tool` (non-empty string), `session` "
        "(non-empty string), `project` (non-empty string -- an unknown name auto-creates a registry stub, "
        "never rejected), `reason` (`\"session_end\"` | `\"daily\"` | `\"manual\"`), `client_ts` (ISO 8601 "
        "timestamp).\n"
        "- Optional: `doctrine_version` (string -- the doctrine version this session bootstrapped under), "
        "`metrics` (object, any subset of `model`/`tokens_in`/`tokens_out`/`cost_estimate`/`duration` -- "
        "absence of the whole object, or of any individual field, is never a violation), `events` (array, "
        "defaults to empty), `handoff` (object, see the handoff-or-waiver rule below), `no_handoff` "
        "(non-empty string, the waiver -- see below), `knowledge` (array, defaults to empty), `documents` "
        "(array, defaults to empty), `project_update` (object -- see \"Updating the project registry\" "
        "below).\n\n"
        f"**`events[]`** -- activity since the last deposit. Each item, required fields: `seq` (integer), "
        "`ts` (ISO 8601 timestamp), `kind` (fixed vocabulary: " + kinds + "), `summary` (non-empty, one "
        "line). Optional: `payload` (any JSON object, capped at 256 KB), `tags` (array of strings, "
        "defaults to empty). An unknown `kind` rejects the *whole* deposit with a `failing_events` list "
        "naming exactly which ones and why. Scripted recovery: relabel the event to `note`, preserve the "
        "original kind as a tag, and resend with the same `deposit_id`.\n\n"
        "**Handoff-or-waiver rule**: a `session_end` deposit must carry either a `handoff` object "
        "(`stands`/`in_flight`/`blocked`/`next_steps` all required strings, `notes` optional) or "
        "`no_handoff: \"<reason>\"` (non-empty string). Silence -- neither present -- is rejected; both "
        "present at once is also rejected, as a contradiction. Deposits with `reason` other than "
        "`session_end` are never required to carry either.\n\n"
        "**Queue-and-retry on unreachable**: if the hub can't be reached, queue the deposit locally and "
        "retry later with the *same* `deposit_id` -- it's the idempotency key, so a retried deposit is "
        "never double-applied, and a retry with a materially different body is still ignored in favor of "
        "the original.\n\n"
        "**`knowledge[]`** -- library entries. Each item is either a new entry or a retire action:\n"
        "- New entry, required: `title` (non-empty string), `namespace` (exactly one of `\"lessons\"` | "
        "`\"howto\"` | `\"reference\"`), `body` (non-empty string, capped at 1 MB). Optional: `tags` "
        "(array of strings, defaults to empty), `supersedes` (**a list** of entry-id strings -- always an "
        "array, even for a single parent; merges name more than one, most entries name zero or one), "
        "`doctrine_proposal` (boolean, defaults to false -- see below), and `project` "
        "(string-or-null, **optional**, cascade rule): omit the `project` key entirely to file the entry "
        "under this deposit's own `project` (the common case -- inherits automatically); send an explicit "
        "`\"project\": null` to file it as universal knowledge instead (belongs to no project, stored with "
        "project NULL, excluded from every project's digest, still fully searchable); send an explicit "
        "project name string to file it under a *different* project than this deposit's own. Omitting the "
        "key and sending `null` are different things -- omit for \"this deposit's project\", send `null` "
        "only when the entry is deliberately meant to be universal.\n"
        '- Retire action instead of a new entry: `{"retire": "<entry id>", "reason": "<non-empty string>"}` '
        "-- closes a wrong/obsolete entry with no replacement, never to reopen a terminal decision. Valid "
        "only against an `active` entry; a bad or already-non-active target is a self-explaining rejection "
        "naming the target and why.\n"
        "Supersession never crosses the proposal boundary in either direction -- an ordinary entry can't "
        "supersede a proposal, and a proposal can't supersede an ordinary entry; proposals are closed via "
        "the owner's approve/reject, not by supersession.\n\n"
        "**Filing a doctrine proposal**: the same `knowledge[]` new-entry shape as a lesson, plus "
        '`"doctrine_proposal": true`. Proposals are stored as ordinary library entries but are never '
        "served at bootstrap and never appear in default/journal/all search -- fetch them explicitly with "
        "`scope=proposals`. The owner reviews via `GET /v1/proposals` and decides via `POST "
        "/v1/proposals/{id}/approve` or `/reject`; approval only *records* the decision, it does not "
        "change doctrine by itself -- promotion into doctrine is the owner's own separate, deliberate "
        "`POST /v1/doctrine/global` or `/v1/doctrine/overlays/{project}`.\n\n"
        "**`documents[]`** -- mirrored ADRs/docs (doctrine mandates ADR written -> next deposit carries "
        'it). Each item, all four fields required, no others recognized: `{"path": "<repo-relative path, '
        'e.g. docs/adr/0003-choose-db.md>", "kind": "adr"|"doc", "title": "<string>", "content": '
        '"<markdown, capped at 1 MB>"}`. Any unrecognized field name rejects the item outright -- a typo\'d '
        "field never silently no-ops. The project's own git repo stays canonical; the Brain just makes "
        "every decision searchable fleet-wide. Supersede-never-erase applies: redepositing the same `path` "
        "never overwrites, it creates the next `version` in that path's history (the ack lists `{path, "
        "version, id}` per item) -- `GET /v1/search?scope=decisions` (or `scope=all` for doc-kind mirrors "
        "too) always surfaces only the latest version per path.\n\n"
        "**Search**: `GET /v1/search?q=&scope=` (machine or owner token). Scopes: `default` (library + "
        "decisions + handoffs), `journal` (adds the events journal on top of default), `all` (default + "
        "journal + doc-kind mirrors -- everything), `decisions` (mirrored ADRs only, latest version per "
        "path), `proposals` (doctrine-proposal library entries only, for reviewing what's pending). "
        "`include_history=true` surfaces superseded/retired library entries too (readers see `active` "
        "content by default; mirrored-document search always shows only the latest version per path -- "
        "prior versions stay stored, supersede-never-erase, but drop out of search). Results paginate via "
        "`cursor`/`next_cursor`, and carry `type`: `library`, `handoff`, `event`, `decision`, or "
        "`document` (the latter two also carry `path`/`version`). Fetch a full library entry (with its "
        "supersession chain and duplicate hints) via `GET /v1/library/{id}`. Curation agents (e.g. the "
        "librarian) also have `GET /v1/flags` (the fork/duplicate queue) and `GET /v1/events` (a filtered, "
        "non-ranked journal read) -- not typically needed by ordinary sessions.\n\n"
        "**Updating the project registry**: a deposit's envelope may carry an optional "
        '`"project_update": {"description"?: "<string>", "status"?: "active"|"paused"|"done"}`, applied '
        "atomically with the rest of the deposit to the deposit's own `project`. Unknown keys or an "
        "invalid `status` reject the whole deposit. The owner can also update a project directly and "
        "deliberately via `PATCH /v1/projects/{name}` (owner token) with the same `{description?, "
        "status?}` shape, independent of any deposit. Read a project's full registry facts (including "
        "current `description`/`status`, which machines have deposited on it, latest handoff, and "
        "counts) via `GET /v1/projects/{name}`; its handoff chain via `GET /v1/projects/{name}/handoffs`.\n\n"
        "**Recovery from a rejected deposit**: every rejection is a `4xx` with body "
        '`{"error": {"code", "detail", ...}}`; validation failures add a `failing_events`/`failing_items` '
        "list naming exactly what to fix. Fix the listed field(s) and resend the *same* `deposit_id` -- "
        "retrying is always safe.\n\n"
        "**Minimal valid example** -- a `\"daily\"` deposit needs no `handoff`/`no_handoff` at all; this "
        "one also files a lesson with `project` omitted, so it inherits `\"my-project\"` per the cascade "
        "rule above:\n"
        "```json\n"
        '{"deposit_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","tool":"claude-code","session":"sess-1",'
        '"project":"my-project","reason":"daily","client_ts":"2026-08-06T12:00:00Z",'
        '"events":[{"seq":1,"ts":"2026-08-06T12:00:00Z","kind":"note","summary":"Did a thing."}],'
        '"knowledge":[{"title":"Example lesson","namespace":"lessons",'
        '"body":"Situation/Problem/Fix/Why it works."}]}\n'
        "```\n\n"
    ) + _notifications_markdown(notification_config)


def _compile_doctrine(global_row: DoctrineVersion | None, overlay_row: DoctrineVersion | None) -> dict:
    """Compiles global + project overlay (contracts-v1.md §6, component 1):
    rules grouped by tier, overridden defaults replaced by overlay text and
    marked, overlay additions appended, then the source markdown. If no
    global doctrine has ever been written, this is honest about it -- never
    fabricates rules.
    """
    if global_row is None:
        return {
            "version_stamp": "none",
            "has_doctrine": False,
            "non_negotiable": [],
            "default": [],
            "overlay_additions": [],
            "global_content": None,
            "overlay_content": None,
        }

    overlay_data = (overlay_row.rules if overlay_row is not None else None) or {}
    override_text_by_id = {o["id"]: o["text"] for o in overlay_data.get("overrides", [])}
    additions = list(overlay_data.get("additions", []))

    non_negotiable: list[dict] = []
    default: list[dict] = []
    for rule in global_row.rules or []:
        if rule["tier"] == "non_negotiable":
            non_negotiable.append({"id": rule["id"], "text": rule["text"]})
        elif rule["id"] in override_text_by_id:
            default.append({"id": rule["id"], "text": override_text_by_id[rule["id"]], "overridden": True})
        else:
            default.append({"id": rule["id"], "text": rule["text"], "overridden": False})

    stamp_parts = [f"global:v{global_row.version}"]
    if overlay_row is not None:
        stamp_parts.append(f"overlay:v{overlay_row.version}")

    return {
        "version_stamp": "+".join(stamp_parts),
        "has_doctrine": True,
        "non_negotiable": non_negotiable,
        "default": default,
        "overlay_additions": additions,
        "global_content": global_row.content,
        "overlay_content": overlay_row.content if overlay_row is not None else None,
    }


def _first_line_snippet(body: str, limit: int = DIGEST_SNIPPET_CHARS) -> str:
    stripped = body.strip()
    first_line = stripped.splitlines()[0] if stripped else ""
    if len(first_line) > limit:
        return first_line[: limit - 1].rstrip() + "…"
    return first_line


def _render_markdown(data: dict) -> str:
    lines: list[str] = [f"# Bootstrap -- {data['project']['name']}", "", f"`doctrine_version: {data['version_stamp']}`", ""]

    doctrine = data["doctrine"]
    lines.append("## 1. Doctrine")
    lines.append("")
    if not doctrine["has_doctrine"]:
        lines.append(
            "No doctrine configured yet -- the owner has not posted a global doctrine "
            "(`POST /v1/doctrine/global`), so there are no rules to report here. This is not an error; it "
            "is an honest statement that doctrine does not exist yet. Proceed using ordinary judgment "
            "until the owner establishes doctrine."
        )
        lines.append("")
    else:
        lines.append("### Non-negotiable (immutable everywhere)")
        lines.append("")
        for r in doctrine["non_negotiable"]:
            lines.append(f"- **{r['id']}**: {r['text']}")
        if not doctrine["non_negotiable"]:
            lines.append("_(none defined)_")
        lines.append("")

        lines.append("### Default (project overlay may override)")
        lines.append("")
        for r in doctrine["default"]:
            if r["overridden"]:
                lines.append(f"- **{r['id']}**: {r['text']} _[project override of {r['id']}]_")
            else:
                lines.append(f"- **{r['id']}**: {r['text']}")
        if not doctrine["default"]:
            lines.append("_(none defined)_")
        lines.append("")

        if doctrine["overlay_additions"]:
            lines.append("### Project-specific additions")
            lines.append("")
            for a in doctrine["overlay_additions"]:
                lines.append(f"- **{a['id']}**: {a['text']}")
            lines.append("")

        lines.append("### Global doctrine (full text)")
        lines.append("")
        lines.append(doctrine["global_content"] or "_(empty)_")
        lines.append("")

        if doctrine["overlay_content"]:
            lines.append(f"### Project overlay (full text) -- {data['project']['name']}")
            lines.append("")
            lines.append(doctrine["overlay_content"])
            lines.append("")

    proj = data["project"]
    lines.append("## 2. Project context")
    lines.append("")
    lines.append(f"- **name**: {proj['name']}")
    lines.append(
        f"- **status**: {proj['status']} _(writable -- via this deposit's `project_update`, or the "
        "owner's `PATCH /v1/projects/{name}`)_"
    )
    lines.append(f"- **description**: {proj['description'] or '_(none)_'} _(writable, same as `status`)_")
    if proj["is_new"]:
        lines.append("")
        lines.append("_New project -- auto-stubbed on this bootstrap fetch. No history exists for it yet._")
    lines.append("")
    lines.append("### Latest handoff")
    lines.append("")
    h = proj["handoff"]
    if h is None:
        lines.append("_No handoff on record yet for this project._")
    else:
        lines.append(f"- **Stands**: {h['stands']}")
        lines.append(f"- **In flight**: {h['in_flight']}")
        lines.append(f"- **Blocked**: {h['blocked']}")
        lines.append(f"- **Next steps**: {h['next_steps']}")
        if h["notes"]:
            lines.append(f"- **Notes**: {h['notes']}")
        lines.append(f"- _received {h['received_at']}_")
    lines.append("")

    lines.append("## 3. Operating instructions")
    lines.append("")
    lines.append(data["operating_instructions"])
    lines.append("")

    lines.append("## 4. Templates")
    lines.append("")
    for name, tmpl in data["templates"].items():
        lines.append(f"### {name}")
        lines.append("")
        lines.append(tmpl)
        lines.append("")

    lines.append("## 5. Lessons digest")
    lines.append("")
    if not data["lessons_digest"]:
        lines.append("_No lessons on record yet for this project._")
    else:
        for item in data["lessons_digest"]:
            lines.append(f"- **{item['title']}** (`{item['id']}`) -- {item['snippet']}")
    lines.append("")

    return "\n".join(lines)


def _markdown_bytes(data: dict) -> int:
    return len(_render_markdown(data).encode("utf-8"))


def _json_bytes(data: dict) -> int:
    # Mirrors starlette's JSONResponse.render exactly (ensure_ascii=False,
    # compact separators) so this measures the actual bytes that will be
    # served, not an approximation.
    return len(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _over_budget(data: dict) -> bool:
    # Both formats are served from the same `data`; the budget binds
    # whichever rendering is larger, markdown or json -- JSON's per-item
    # struct overhead (quoted keys, braces) can push it over even when the
    # markdown rendering of the same data is comfortably under budget.
    return _markdown_bytes(data) > SIZE_BUDGET_BYTES or _json_bytes(data) > SIZE_BUDGET_BYTES


def _apply_size_budget(data: dict, *, machine_id: str, project: str) -> dict:
    if not _over_budget(data):
        return data

    logger.warning(
        "bootstrap response for project=%s machine=%s is %d markdown bytes / %d json bytes, over the %d "
        "byte budget; trimming the lessons digest first",
        project,
        machine_id,
        _markdown_bytes(data),
        _json_bytes(data),
        SIZE_BUDGET_BYTES,
    )
    while data["lessons_digest"] and _over_budget(data):
        data["lessons_digest"].pop()

    if _over_budget(data):
        logger.warning(
            "bootstrap response for project=%s machine=%s still over budget after emptying the lessons "
            "digest; dropping overlay content next",
            project,
            machine_id,
        )
        data["doctrine"]["overlay_content"] = None

    if _over_budget(data):
        logger.warning(
            "bootstrap response for project=%s machine=%s still exceeds the %d byte budget after trimming "
            "the digest and overlay content; non-negotiable rules, default rules, and global doctrine "
            "content are never trimmed, so this response is being served oversized",
            project,
            machine_id,
            SIZE_BUDGET_BYTES,
        )
    return data


@router.get("")
async def get_bootstrap(
    project: str = Query(..., min_length=1),
    format: str | None = Query(default=None),
    principal: Principal = Depends(require_machine),
    db: AsyncSession = Depends(get_db),
):
    machine_id = principal.machine.id

    # Unknown project -> auto-stub (same behavior as deposits), never rejected.
    project_row = await db.get(Project, project)
    is_new = False
    if project_row is None:
        project_row = Project(name=project, status="active", created_at=datetime.now(UTC))
        db.add(project_row)
        is_new = True
        await db.flush()

    global_row = await current_global(db)
    overlay_row = await current_overlay(db, project)
    notification_config = await current_notification_config(db)

    latest_handoff = await db.scalar(
        select(Handoff).where(Handoff.project == project).order_by(Handoff.received_at.desc()).limit(1)
    )

    digest_rows = (
        await db.scalars(
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.project == project,
                KnowledgeEntry.status == "active",
                KnowledgeEntry.is_doctrine_proposal.is_(False),
            )
            .order_by(KnowledgeEntry.created_at.desc())
            .limit(DIGEST_LIMIT)
        )
    ).all()

    doctrine = _compile_doctrine(global_row, overlay_row)

    data = {
        "version_stamp": doctrine["version_stamp"],
        "doctrine": doctrine,
        "project": {
            "name": project_row.name,
            "status": project_row.status,
            "description": project_row.description,
            "is_new": is_new,
            "handoff": None
            if latest_handoff is None
            else {
                "stands": latest_handoff.stands,
                "in_flight": latest_handoff.in_flight,
                "blocked": latest_handoff.blocked,
                "next_steps": latest_handoff.next_steps,
                "notes": latest_handoff.notes,
                "received_at": latest_handoff.received_at.isoformat(),
            },
        },
        "operating_instructions": _operating_instructions_markdown(notification_config),
        "templates": TEMPLATES,
        "lessons_digest": [
            {"id": e.id, "title": e.title, "snippet": _first_line_snippet(e.body)} for e in digest_rows
        ],
    }

    data = _apply_size_budget(data, machine_id=machine_id, project=project)

    db.add(
        BootstrapFetch(
            id=str(ULID()),
            machine_id=machine_id,
            project=project,
            doctrine_global_version=global_row.version if global_row is not None else None,
            doctrine_overlay_version=overlay_row.version if overlay_row is not None else None,
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()

    if format == "json":
        return JSONResponse(content=data)
    return PlainTextResponse(content=_render_markdown(data), media_type="text/markdown")
