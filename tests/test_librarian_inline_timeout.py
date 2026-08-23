"""app/routers/librarian.py's `effective_inline_run_timeout_secs` -- the
wrapper timeout on the SYNCHRONOUS `POST /v1/librarian/run` request. A real
review flagged that raising `LLM_CALL_TIMEOUT_SECS` (app/config.py, this
PR's own earlier change) made the previous flat 600s wrapper
disproportionately short: a run can make up to `max_llm_calls` SEQUENTIAL
judgment calls, each now allowed up to `llm_call_timeout_secs`, so as few
as ~4 slow-but-SUCCESSFUL calls against a local/reasoning model could
exhaust a flat 600s budget even though nothing was actually failing.

This file covers the derivation formula (unit-level, via
`LibrarianLimits`/a monkeypatched `get_settings`) and the settings-level
validation for the explicit override (`LIBRARIAN_INLINE_RUN_TIMEOUT_SECS`,
app/config.py). tests/test_librarian_api.py covers the end-to-end request/
response/history-row behavior when the wrapper actually trips.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.routers.librarian as librarian_router_module
from app.config import Settings
from app.librarian_engine import LibrarianLimits
from app.routers.librarian import (
    INLINE_RUN_TIMEOUT_CEILING_SECS,
    INLINE_RUN_TIMEOUT_FLOOR_SECS,
    effective_inline_run_timeout_secs,
)


def _limits(*, max_llm_calls: int, call_timeout_secs: float) -> LibrarianLimits:
    return LibrarianLimits(max_llm_calls=max_llm_calls, call_timeout_secs=call_timeout_secs)


def _no_override(monkeypatch) -> None:
    monkeypatch.setattr(librarian_router_module, "get_settings", lambda: SimpleNamespace(librarian_inline_run_timeout_secs=None))


# --- derivation: max_llm_calls * call_timeout_secs, clamped [floor, ceiling] ---


def test_derivation_mid_range_no_clamping(monkeypatch):
    """100 calls * 180s (the app defaults) = 18000s, well above the 3600s
    ceiling -- clamped down. Pick a combination that lands strictly between
    the floor and ceiling to prove the *formula* itself, not just the
    clamps.
    """
    _no_override(monkeypatch)
    limits = _limits(max_llm_calls=10, call_timeout_secs=100.0)  # 10 * 100 = 1000s

    result = effective_inline_run_timeout_secs(limits)

    assert result == 1000.0
    assert INLINE_RUN_TIMEOUT_FLOOR_SECS < result < INLINE_RUN_TIMEOUT_CEILING_SECS


def test_derivation_another_mid_range_combination(monkeypatch):
    _no_override(monkeypatch)
    limits = _limits(max_llm_calls=20, call_timeout_secs=60.0)  # 20 * 60 = 1200s

    result = effective_inline_run_timeout_secs(limits)

    assert result == 1200.0


def test_derivation_app_defaults_are_clamped_to_the_ceiling(monkeypatch):
    """The actual shipped defaults (max_llm_calls=100,
    llm_call_timeout_secs=180.0 -> 18000s derived) must land exactly on the
    ceiling, not the raw product -- this is the real-world case the review
    flagged.
    """
    _no_override(monkeypatch)
    limits = _limits(max_llm_calls=100, call_timeout_secs=180.0)  # 18000s raw

    result = effective_inline_run_timeout_secs(limits)

    assert result == INLINE_RUN_TIMEOUT_CEILING_SECS == 3600.0


def test_derivation_floor_applies_for_a_small_budget(monkeypatch):
    """A small max_llm_calls/call_timeout_secs product (e.g. a tight
    LibrarianLimits used in tests, or a conservative deployment) must never
    derive to LESS than the previous flat 600s default.
    """
    _no_override(monkeypatch)
    limits = _limits(max_llm_calls=3, call_timeout_secs=30.0)  # 90s raw -- well under the floor

    result = effective_inline_run_timeout_secs(limits)

    assert result == INLINE_RUN_TIMEOUT_FLOOR_SECS == 600.0


def test_derivation_ceiling_applies_for_a_huge_budget(monkeypatch):
    _no_override(monkeypatch)
    limits = _limits(max_llm_calls=1000, call_timeout_secs=1800.0)  # 1,800,000s raw

    result = effective_inline_run_timeout_secs(limits)

    assert result == INLINE_RUN_TIMEOUT_CEILING_SECS == 3600.0


# --- explicit override wins outright, bypassing derivation entirely ---


def test_explicit_override_wins_over_derivation(monkeypatch):
    monkeypatch.setattr(
        librarian_router_module, "get_settings", lambda: SimpleNamespace(librarian_inline_run_timeout_secs=222.0)
    )
    # A limits combination that would derive to something else entirely if
    # the override weren't honored -- proves the override truly short-
    # circuits the formula rather than just coincidentally matching it.
    limits = _limits(max_llm_calls=100, call_timeout_secs=180.0)

    result = effective_inline_run_timeout_secs(limits)

    assert result == 222.0


def test_explicit_override_can_be_below_the_derivation_floor(monkeypatch):
    """An explicit override is the owner's deliberate choice -- unlike the
    derived default, it is NOT floored at 600s.
    """
    monkeypatch.setattr(
        librarian_router_module, "get_settings", lambda: SimpleNamespace(librarian_inline_run_timeout_secs=90.0)
    )
    limits = _limits(max_llm_calls=100, call_timeout_secs=180.0)

    result = effective_inline_run_timeout_secs(limits)

    assert result == 90.0


# --- app/config.py: LIBRARIAN_INLINE_RUN_TIMEOUT_SECS validation ---


def test_settings_default_is_none_meaning_derive(monkeypatch):
    monkeypatch.delenv("LIBRARIAN_INLINE_RUN_TIMEOUT_SECS", raising=False)

    settings = Settings()

    assert settings.librarian_inline_run_timeout_secs is None


def test_settings_blank_env_value_treated_as_unset(monkeypatch):
    """docker-compose forwards this var as `${LIBRARIAN_INLINE_RUN_TIMEOUT_SECS:-}`
    -- an empty string when unset in .env, same as every other optional var
    in this app -- which must mean "derive", not a float-parsing failure at
    startup.
    """
    monkeypatch.setenv("LIBRARIAN_INLINE_RUN_TIMEOUT_SECS", "")

    settings = Settings()

    assert settings.librarian_inline_run_timeout_secs is None


def test_settings_explicit_value_accepted(monkeypatch):
    monkeypatch.setenv("LIBRARIAN_INLINE_RUN_TIMEOUT_SECS", "900")

    settings = Settings()

    assert settings.librarian_inline_run_timeout_secs == 900.0


@pytest.mark.parametrize("value", ["0", "10", "59.9", "7200.1", "100000", "-5"])
def test_settings_out_of_range_rejected_at_construction(monkeypatch, value):
    monkeypatch.setenv("LIBRARIAN_INLINE_RUN_TIMEOUT_SECS", value)

    with pytest.raises(ValidationError, match="librarian_inline_run_timeout_secs"):
        Settings()


@pytest.mark.parametrize("value", ["60", "7200", "600"])
def test_settings_boundary_values_accepted(monkeypatch, value):
    monkeypatch.setenv("LIBRARIAN_INLINE_RUN_TIMEOUT_SECS", value)

    settings = Settings()

    assert settings.librarian_inline_run_timeout_secs == float(value)
