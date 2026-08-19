"""Room mode definitions (app/room_modes.py, ADR-0007) -- pure unit tests,
no DB/HTTP round trip needed. app/rooms.py's create-time validation and
app/onboarding.py's join-prompt injection are exercised end-to-end
elsewhere (tests/test_rooms.py, tests/test_onboarding.py); this file only
covers the single-source mode table itself.
"""

import pytest

from app.errors import ApiError
from app.room_modes import (
    DEFAULT_MODE,
    ROOM_MODES,
    VALID_MODES,
    closing_instruction_for,
    role_text_for,
    validate_mode,
)


def test_default_mode_is_freeform():
    assert DEFAULT_MODE == "freeform"


def test_valid_modes_are_the_five_expected():
    assert VALID_MODES == {"freeform", "debate", "collaborate", "brainstorm", "critique"}


def test_freeform_is_symmetric_with_no_sides_and_no_role_text():
    mode_def = ROOM_MODES["freeform"]
    assert mode_def.symmetric is True
    assert mode_def.sides is None
    assert mode_def.role_text is None
    assert mode_def.closing_instruction is None
    assert role_text_for("freeform", None, "anything", "partner") is None
    assert closing_instruction_for("freeform", None) is None


def test_debate_is_asymmetric_with_for_against_sides():
    mode_def = ROOM_MODES["debate"]
    assert mode_def.symmetric is False
    assert mode_def.sides == ("for", "against")


def test_critique_is_asymmetric_with_proposer_critic_sides():
    mode_def = ROOM_MODES["critique"]
    assert mode_def.symmetric is False
    assert mode_def.sides == ("proposer", "critic")


def test_collaborate_and_brainstorm_are_symmetric_with_no_sides():
    for mode in ("collaborate", "brainstorm"):
        mode_def = ROOM_MODES[mode]
        assert mode_def.symmetric is True
        assert mode_def.sides is None


def test_validate_mode_accepts_all_valid_modes():
    for mode in VALID_MODES:
        validate_mode(mode)  # must not raise


def test_validate_mode_rejects_unknown_mode():
    with pytest.raises(ApiError) as exc_info:
        validate_mode("nonsense")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_room_mode"


def test_debate_role_text_for_side():
    text = role_text_for("debate", "for", "cats are better than dogs", "Rival")
    assert "You argue FOR the proposition: cats are better than dogs." in text
    assert "rebut your opponent" in text


def test_debate_role_text_against_side():
    text = role_text_for("debate", "against", "cats are better than dogs", "Rival")
    assert "You argue AGAINST the proposition: cats are better than dogs." in text


def test_critique_role_text_proposer_vs_critic_differ():
    proposer_text = role_text_for("critique", "proposer", "the new API design", "Reviewer")
    critic_text = role_text_for("critique", "critic", "the new API design", "Reviewer")
    assert "Present and defend your proposal: the new API design." in proposer_text
    assert "Stress-test and red-team the proposal: the new API design." in critic_text
    assert proposer_text != critic_text


def test_critique_closing_instruction_differs_by_side():
    proposer_closing = closing_instruction_for("critique", "proposer")
    critic_closing = closing_instruction_for("critique", "critic")
    assert "revised proposal" in proposer_closing
    assert "remaining concerns" in critic_closing
    assert proposer_closing != critic_closing


def test_collaborate_role_text_mentions_partner_and_topic():
    text = role_text_for("collaborate", None, "ship the v2 API", "Ally")
    assert "Collaborate with Ally to ship the v2 API." in text


def test_brainstorm_role_text_mentions_partner_and_topic():
    text = role_text_for("brainstorm", None, "growth ideas", "Ally")
    assert "Brainstorm ideas about growth ideas with Ally." in text
