"""Room modes -- freeform/debate/collaborate/brainstorm/critique (ADR-0007,
extends ADR-0006's agent chat rooms).

Single source of truth for mode text, mirroring app/roles.py's pattern
("kept here once so the two surfaces can never drift"): used by BOTH
app/rooms.py (create-time validation: non-freeform requires a topic,
asymmetric modes require the two members assigned the mode's two distinct
sides) and the join prompt generator (app/onboarding.py's
`generate_room_join_prompt`, which injects a mode's per-side role text and
closing instruction with the topic/partner filled in).

'freeform' (the default) is symmetric, has no sides, and contributes no
role-text/closing-instruction paragraph -- the join prompt's existing
generic conversational framing stands unchanged, same as 'solo' contributing
no paragraph in app/roles.py.

Asymmetric modes (debate, critique) each define exactly two distinct side
keys (`sides`); create_room (app/rooms.py) must assign the mode's two sides,
one each, across the room's two members. Symmetric modes (collaborate,
brainstorm) have `sides=None` and ignore any side assignment.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.errors import ApiError

DEFAULT_MODE = "freeform"


@dataclass(frozen=True)
class RoomMode:
    label: str
    symmetric: bool
    # The two distinct side keys for an asymmetric mode (e.g. ('for',
    # 'against')); None for symmetric/freeform modes, which have no sides.
    sides: tuple[str, str] | None
    # Side key -> human display label, for asymmetric modes only.
    side_labels: dict[str, str] | None
    # (side, topic, partner) -> this agent's role text with topic/partner
    # filled in. `side` is None for symmetric modes. None for freeform (no
    # special stance text -- the join prompt's generic framing stands).
    role_text: Callable[[str | None, str, str], str] | None
    # side -> the closing-statement wrap-up instruction. `side` is None for
    # symmetric modes. None for freeform.
    closing_instruction: Callable[[str | None], str] | None


def _debate_role_text(side: str | None, topic: str, partner: str) -> str:
    if side == "for":
        return (
            f"You argue FOR the proposition: {topic}. Make the strongest case for it, rebut your opponent, "
            "stay substantive."
        )
    return (
        f"You argue AGAINST the proposition: {topic}. Make the strongest case against it, rebut your "
        "opponent, stay substantive."
    )


def _debate_closing(_side: str | None) -> str:
    return "As the deadline nears (you'll get a system notice), post a closing statement summarizing your strongest points."


def _collaborate_role_text(_side: str | None, topic: str, partner: str) -> str:
    return f"Collaborate with {partner} to {topic}. Build on each other's contributions and converge on the best result."


def _collaborate_closing(_side: str | None) -> str:
    return "When wrapping up, post a short summary of what you concluded or produced together."


def _brainstorm_role_text(_side: str | None, topic: str, partner: str) -> str:
    return (
        f"Brainstorm ideas about {topic} with {partner}. Go wide, defer judgment, build on each other's "
        "ideas rather than critiquing them."
    )


def _brainstorm_closing(_side: str | None) -> str:
    return "When wrapping up, post a consolidated list of the best ideas."


def _critique_role_text(side: str | None, topic: str, partner: str) -> str:
    if side == "proposer":
        return f"Present and defend your proposal: {topic}. Respond to the critic's objections and refine it."
    return (
        f"Stress-test and red-team the proposal: {topic}. Find flaws, edge cases, and failure modes; be "
        "rigorous but fair."
    )


def _critique_closing(side: str | None) -> str:
    if side == "proposer":
        return "When wrapping up, post a revised proposal accounting for the critique."
    return "When wrapping up, post your top remaining concerns."


ROOM_MODES: dict[str, RoomMode] = {
    "freeform": RoomMode(
        label="Freeform",
        symmetric=True,
        sides=None,
        side_labels=None,
        role_text=None,
        closing_instruction=None,
    ),
    "debate": RoomMode(
        label="Debate",
        symmetric=False,
        sides=("for", "against"),
        side_labels={"for": "For", "against": "Against"},
        role_text=_debate_role_text,
        closing_instruction=_debate_closing,
    ),
    "collaborate": RoomMode(
        label="Collaborate",
        symmetric=True,
        sides=None,
        side_labels=None,
        role_text=_collaborate_role_text,
        closing_instruction=_collaborate_closing,
    ),
    "brainstorm": RoomMode(
        label="Brainstorm",
        symmetric=True,
        sides=None,
        side_labels=None,
        role_text=_brainstorm_role_text,
        closing_instruction=_brainstorm_closing,
    ),
    "critique": RoomMode(
        label="Critique",
        symmetric=False,
        sides=("proposer", "critic"),
        side_labels={"proposer": "Proposer", "critic": "Critic"},
        role_text=_critique_role_text,
        closing_instruction=_critique_closing,
    ),
}

VALID_MODES = frozenset(ROOM_MODES)


def validate_mode(mode: str) -> None:
    """Self-explaining rejection for an unrecognized mode -- same envelope
    shape as app/roles.py's `validate_role`.
    """
    if mode not in ROOM_MODES:
        raise ApiError(
            422,
            "invalid_room_mode",
            f"`mode` must be one of {sorted(VALID_MODES)}, got {mode!r}. Recovery: fix the field and resend.",
        )


def role_text_for(mode: str, side: str | None, topic: str, partner: str) -> str | None:
    """This mode+side's role text with topic/partner filled in, or None for
    freeform (no special stance text to inject).
    """
    fn = ROOM_MODES[mode].role_text
    if fn is None:
        return None
    return fn(side, topic, partner)


def closing_instruction_for(mode: str, side: str | None) -> str | None:
    """This mode+side's closing-statement wrap-up instruction, or None for
    freeform.
    """
    fn = ROOM_MODES[mode].closing_instruction
    if fn is None:
        return None
    return fn(side)
