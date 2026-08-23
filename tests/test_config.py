"""app/config.py -- Settings, focused on the LLM call/test timeout knobs
(LLM_CALL_TIMEOUT_SECS/LLM_TEST_TIMEOUT_SECS). A real deployment hit a
previously-hardcoded 30s timeout with a local Ollama REASONING model (one
that emits a long chain-of-thought before any content) on an ordinary room
transcript -- these are now owner-configurable, bounded 5-1800 seconds, and
must fail clearly at process startup when out of range rather than being
silently clamped or accepted.

`Settings()` is constructed directly here (not via the process-wide
`get_settings()` lru_cache) so each test gets a fresh read of whatever env
vars it set -- `DATABASE_URL` is already present in the test environment
(see tests/conftest.py's module docstring), so the only vars under test are
the two LLM timeout ones.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_llm_timeout_defaults(monkeypatch):
    monkeypatch.delenv("LLM_CALL_TIMEOUT_SECS", raising=False)
    monkeypatch.delenv("LLM_TEST_TIMEOUT_SECS", raising=False)

    settings = Settings()

    assert settings.llm_call_timeout_secs == 180.0
    assert settings.llm_test_timeout_secs == 60.0


@pytest.mark.parametrize("value", ["0", "4.9", "-10", "1800.1", "5000"])
def test_llm_call_timeout_out_of_range_rejected_at_construction(monkeypatch, value):
    monkeypatch.setenv("LLM_CALL_TIMEOUT_SECS", value)

    with pytest.raises(ValidationError, match="llm_call_timeout_secs"):
        Settings()


@pytest.mark.parametrize("value", ["0", "4.9", "-10", "1800.1", "5000"])
def test_llm_test_timeout_out_of_range_rejected_at_construction(monkeypatch, value):
    monkeypatch.setenv("LLM_TEST_TIMEOUT_SECS", value)

    with pytest.raises(ValidationError, match="llm_test_timeout_secs"):
        Settings()


@pytest.mark.parametrize("value", ["5", "1800", "180", "60.5"])
def test_llm_timeout_boundary_and_ordinary_values_accepted(monkeypatch, value):
    monkeypatch.setenv("LLM_CALL_TIMEOUT_SECS", value)
    monkeypatch.setenv("LLM_TEST_TIMEOUT_SECS", value)

    settings = Settings()

    assert settings.llm_call_timeout_secs == float(value)
    assert settings.llm_test_timeout_secs == float(value)


def test_llm_call_timeout_non_numeric_value_rejected(monkeypatch):
    monkeypatch.setenv("LLM_CALL_TIMEOUT_SECS", "not-a-number")

    with pytest.raises(ValidationError):
        Settings()
