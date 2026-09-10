"""Room-setup briefing -- a generic, zero-fleet-data format spec for an
external LLM that cannot reach Brainard (ADR-0019 decision 2).

**Revised from the original, live-data-driven framing during ADR review.**
This module contains NO database access of any kind, and never will: the
owner explicitly rejected a briefing built from live fleet data ("I dont
want any external agent to know anything about my machines etc. just
understand the format") because he is often mid-conversation with an
external LLM (Gemini, ChatGPT) about some unrelated topic and does not want
that LLM to learn anything about his infrastructure -- no machine name, no
agent name, no group name, no project name, not even as an illustrative
example. What this module produces is a pure description of the *shape*
and *constraints* of a room configuration, generated once, at import time,
from app/room_modes.py's `ROOM_MODES` and app/rooms.py's module-level
validator constants -- the exact same "static, trusted, never per-request"
posture `ROOM_MODES_JSON` already has (app/routers/ui_rooms.py). Because it
depends on nothing but those constants, it is computed exactly once here,
never per-request, and is safe to paste into literally any external LLM
conversation, regardless of what that LLM already knows -- there is nothing
fleet-specific in it to know.

Anti-drift, not hand-maintained prose with its own numbers: every number
and enum below is read straight from the same constants `create_room`
itself enforces (app/rooms.py) and the same `ROOM_MODES` the create-room
form's own mode dropdown uses (app/room_modes.py) -- never restated. A
future change to a bound, a new mode, or a changed consensus-floor rule
flows into the next-served copy of this text automatically (on next process
restart, the same freshness bound `ROOM_MODES_JSON` already accepts). The
English sentences wrapping those facts remain hand-written and must still
be kept honest by whoever changes the underlying rule -- the same residual
risk every other generated-prompt docstring in this codebase already
carries (app/onboarding.py's generators are the precedent).

Rendered into `rooms_list.html` inside a hidden
`<pre id="room-setup-briefing-text" hidden>` element and copied via the
existing three-tier clipboard handler (app/static/main.js, ADR-0013
decision 5) -- no second copy path (ADR-0019 decision 5).

**2026-09-10 revision (ADR-0019 Consequences update):** two fields changed
based on owner feedback that pastes were coming back with blank agent names
and no way to actually start the room.

1. `agent_a`/`agent_b` are no longer "leave blank." They were never fleet
   data in the first place -- they are the room's two free-text MEMBER
   labels, unrelated to which of the owner's real machines eventually staffs
   either seat (that's ADR-0018's separate, owner-only seat assignment,
   which this briefing still says nothing about). The briefing now asks the
   model to propose short, ROLE-DESCRIPTIVE names for the two members, drawn
   from the topic and mode already in front of it -- e.g. "Proposer"/
   "Critic" for a critique, "Contract Advocate"/"Risk Reviewer" for a
   contract debate. This is still zero-fleet-data: a role label invented
   from the topic discloses nothing about the owner's actual infrastructure,
   which is the only thing decision 2 ever promised to withhold.
2. A new eleventh field, `opening_message`, asks the model to draft the
   actual first message for the room -- a substantive framing of what the
   two agents are to work through, grounded in the conversation already
   underway, not a restatement of the one-line topic. ADR-0014 gates a room
   until the owner posts; before this field existed, the owner had to
   create the room and then go type that first message by hand every time.
   `opening_message`, when non-blank, is now posted by `app.routers.ui_rooms
   .rooms_create` as the owner's own first message immediately after
   `create_room` succeeds -- through the ordinary `post_message` path, same
   validation, same row lock, same cap -- which is exactly what opens the
   room per ADR-0014.

The sentinel is deliberately still `BRAINARD-ROOM-SETUP-v1`, not bumped to
v2, even though the reply shape gained a field: an older pasted reply
(produced by a previous copy of this briefing, before `opening_message`
existed) simply never contains that key, and an absent key has always meant
"leave that field alone" (decision 3, point 8) -- nothing about parsing an
old reply changes. Bumping the sentinel would have made the parser
(app/static/room_setup_paste.js) *reject* that still-perfectly-valid old
reply outright (it searches for the literal sentinel text; a `v2` parser
would simply never find a `v1` block), breaking backward compatibility to
enforce a version distinction the field's own optionality already makes
unnecessary.
"""

from app.room_modes import DEFAULT_MODE, ROOM_MODES
from app.rooms import (
    CONSENSUS_FLOOR_MAX,
    CONSENSUS_FLOOR_MIN,
    DEFAULT_CONSENSUS_FLOOR,
    DEFAULT_MAX_MESSAGES,
    MAX_DURATION_SECONDS,
    MAX_MESSAGES_MAX,
    MAX_MESSAGES_MIN,
)

# The eleven fixed keys of the reply format (ADR-0019 decision 3, extended by
# the 2026-09-10 revision above with `opening_message`), in the exact order
# the template presents them -- a plain module-level list (not just implied
# by the template string below) so tests, and any future consumer, can
# assert against the single, real list of recognized keys rather than
# re-deriving it by re-parsing the template text.
REPLY_FORMAT_KEYS: list[str] = [
    "name",
    "mode",
    "topic",
    "agent_a",
    "agent_b",
    "max_messages",
    "consensus_floor",
    "group",
    "project",
    "duration_minutes",
    "opening_message",
]

REPLY_FORMAT_SENTINEL_START = "BRAINARD-ROOM-SETUP-v1"
REPLY_FORMAT_SENTINEL_END = "END-BRAINARD-ROOM-SETUP"

# The fixed, versioned, sentinel-delimited reply template (ADR-0019 decision
# 3) -- the "real delimiter" is the sentinel pair, not the surrounding
# fence; a leading/trailing paragraph of LLM commentary or a differently
# rendered fence is tolerated by the parser (app/static/room_setup_paste.js)
# either way. Kept as its own constant, separate from the surrounding
# briefing prose, so both this module and that parser can be tested against
# the identical, single source of the format's shape.
REPLY_FORMAT_TEMPLATE = (
    f"{REPLY_FORMAT_SENTINEL_START}\n"
    "name: <value or blank>\n"
    "mode: <value or blank>\n"
    "topic: <value or blank>\n"
    "agent_a: <value>\n"
    "agent_b: <value>\n"
    "max_messages: <value or blank>\n"
    "consensus_floor: <value or blank>\n"
    "group: <leave blank>\n"
    "project: <leave blank>\n"
    "duration_minutes: <value or blank>\n"
    "opening_message: <value or blank -- MUST be the last field; may span multiple lines>\n"
    f"{REPLY_FORMAT_SENTINEL_END}"
)

_MAX_DURATION_MINUTES = MAX_DURATION_SECONDS // 60


def _describe_mode(key: str) -> str:
    """One bullet for one mode, generated entirely from `ROOM_MODES[key]` --
    label, symmetric-vs-sides, and (for an asymmetric mode) the ordering
    rule create_room's own `_sides_for_mode` (app/routers/ui_rooms.py)
    actually applies. No fleet data of any kind can appear here: `ROOM_MODES`
    carries only static, server-authored product text (labels, side names),
    never a database row.
    """
    mode = ROOM_MODES[key]
    if mode.symmetric:
        return f"  - \"{key}\" ({mode.label}): symmetric -- no sides, no adversarial framing."
    sides = mode.sides
    side_labels = mode.side_labels
    assert sides is not None and side_labels is not None  # asymmetric modes always define both
    first, second = sides
    return (
        f"  - \"{key}\" ({mode.label}): asymmetric, with two sides -- {side_labels[first]}/{side_labels[second]}. "
        "Whichever of the room's two agent names is listed first is assigned this mode's first side "
        f"({side_labels[first]}), the second name gets the second side ({side_labels[second]})."
    )


_MODES_BLOCK = "\n".join(_describe_mode(key) for key in ROOM_MODES)
_VALID_MODE_NAMES = ", ".join(f'"{key}"' for key in ROOM_MODES)

ROOM_SETUP_BRIEFING = f"""You are helping me prepare the configuration for a "room" in a private tool I run \
called Brainard. You don't need any access to it for this -- I just need this one thing translated into a \
fixed format.

A room is a structured, bounded chat session between two AI agents I already have set up, working through a \
topic, shaped by a "mode" that determines how they relate to each other.

The valid modes, and what each means:
{_MODES_BLOCK}

The fields a room configuration needs, and what each means:

  - name: required, non-empty. A short, human-readable label for the room.
  - mode: one of the modes above ({_VALID_MODE_NAMES}); defaults to "{DEFAULT_MODE}" if left blank.
  - topic: required and non-empty for every mode except "{DEFAULT_MODE}"; ignored (optional) for \
"{DEFAULT_MODE}".
  - agent_a / agent_b: propose short, role-descriptive names for the room's two participants, based on the \
topic and mode -- e.g. "Proposer"/"Critic" for a critique, "Contract Advocate"/"Risk Reviewer" for a contract \
debate. These are just the room's two member LABELS, not real identities of any kind -- for an asymmetric \
mode (one with sides, see above), whichever name you list first is assigned that mode's first side, so pick \
names that read naturally against those two sides specifically, in that order; for a symmetric mode, pick \
names that fit whatever two distinct roles the topic naturally implies. Invent these freely -- you have no \
way to know (and must not guess) which of my own AI agents will actually staff either one; I assign that \
separately myself, after you reply.
  - max_messages: an integer between {MAX_MESSAGES_MIN} and {MAX_MESSAGES_MAX}; defaults to \
{DEFAULT_MAX_MESSAGES} if left blank.
  - consensus_floor: an integer between {CONSENSUS_FLOOR_MIN} and {CONSENSUS_FLOOR_MAX}; defaults to \
{DEFAULT_CONSENSUS_FLOOR} if left blank. It only has an effect for the two adversarial modes (the ones with \
sides above): before either agent can close such a room as agreed, at least this many messages must have been \
posted AND each agent must have posted at least one objection. It must be strictly less than max_messages, or \
the room could hit its message cap before an agreed close ever gets a chance. For every other mode it is \
stored but has no effect.
  - group: LEAVE BLANK. This is a free-form, owner-facing label I use only to filter and bulk-manage my own \
list of rooms -- it has no effect on the room's conversation, and you have no way to know what labels I \
already use.
  - project: LEAVE BLANK. This is a free-form identity string that the room's own agents compare, exactly, \
against the project they are each currently working on, as a safety check before they join -- a mismatch \
tells an agent to stop and not join. You have no way to know my project naming, and guessing one risks a real \
agent later being told, correctly, to stay out of a room it should actually be in.
  - a time limit: either no limit, or a duration (expressed below in whole minutes, for simplicity), bounded \
between 1 minute and {_MAX_DURATION_MINUTES} minutes (30 days).
  - opening_message: draft the actual first message to post in the room -- a substantive framing of what the \
two agents are to work through, grounded in what we've actually discussed here, NOT a restatement of the \
topic line above. This is what starts the room: I won't let either agent begin until this message (or another \
one from me) is posted, so write it as if you were opening the conversation yourself. It MUST be the LAST \
field in your reply -- unlike every other field above, its value is allowed to run across multiple lines or \
paragraphs, and everything from "opening_message:" to the end of the block is treated as part of it, not as \
more fields. Leave it blank (or omit it) if there's nothing yet worth opening with; I'll write and post it \
myself.

Reply with ONLY a block in exactly this format (wrap it in a fenced code block for readability, but the \
BRAINARD-ROOM-SETUP-v1 / END-BRAINARD-ROOM-SETUP lines themselves are what actually matters, not the fence):

```
{REPLY_FORMAT_TEMPLATE}
```

Fill in whatever you can genuinely infer from our conversation so far (name, mode, topic, and -- only if it's \
implied by what we've discussed -- max_messages, consensus_floor, or the time limit), propose role-descriptive \
agent_a/agent_b names as described above, and draft opening_message as described above (or leave it blank if \
there's nothing yet worth opening with). For anything you cannot genuinely infer, leave the value blank after \
the colon rather than inventing, guessing, or placeholder-filling it -- a blank value just means I'll fill that \
field in myself. In particular, always leave group and project blank, as described above."""
