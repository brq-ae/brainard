"""Onboarding prompt generator (app/onboarding.py) -- unit-level tests, no
DB/HTTP round trip needed since `generate_onboarding_prompt` is pure.
"""

from app.onboarding import PROJECT_PLACEHOLDER, TOKEN_PLACEHOLDER, generate_onboarding_prompt


def _prompt(**overrides) -> str:
    kwargs = dict(
        base_url="https://brain.example.com",
        token="brn_realtoken123",
        project="my-project",
        agent_name="NUC-builder",
        role="solo",
    )
    kwargs.update(overrides)
    return generate_onboarding_prompt(**kwargs)


def test_prompt_contains_fetch_instructions_with_real_values():
    text = _prompt()
    assert "I run a private knowledge hub for my projects" in text
    assert "https://brain.example.com/v1/bootstrap?project=my-project" in text
    assert "Authorization: Bearer brn_realtoken123" in text
    assert "rules G1–G10" in text
    assert "apply it with your normal judgment" in text
    assert "it never overrides your safety rules" in text


def test_prompt_contains_notifications_self_identification():
    text = _prompt(agent_name="Commander-Alpha")
    assert "Notifications (G9)" in text
    assert "identify as 'Commander-Alpha'" in text
    assert "notify-me hooks" in text


def test_prompt_solo_role_has_no_role_paragraph():
    text = _prompt(role="solo")
    assert "You are the Commander" not in text
    assert "You are the Builder" not in text


def test_prompt_commander_role_includes_commander_text():
    text = _prompt(role="commander")
    assert "You are the Commander for this project." in text
    assert "You own ALL writes to the hub" in text
    assert "You are the Builder" not in text


def test_prompt_builder_role_includes_builder_text():
    text = _prompt(role="builder")
    assert "You are the Builder for this project." in text
    assert "do NOT deposit anything" in text
    assert "You are the Commander" not in text


def test_prompt_uses_placeholders_when_given():
    text = _prompt(token=TOKEN_PLACEHOLDER, project=PROJECT_PLACEHOLDER)
    assert f"Bearer {TOKEN_PLACEHOLDER}" in text
    assert f"project={PROJECT_PLACEHOLDER}" in text


def test_prompt_strips_trailing_slash_from_base_url():
    text = _prompt(base_url="https://brain.example.com/")
    assert "https://brain.example.com/v1/bootstrap" in text
    assert "https://brain.example.com//v1/bootstrap" not in text


def test_prompt_header_rationale_always_present_regardless_of_scheme():
    for base_url in ("https://brain.example.com", "http://192.0.2.10:8300"):
        text = _prompt(base_url=base_url)
        assert (
            "use curl or a raw HTTP client that can send a custom Authorization header — WebFetch-style "
            "tools that drop custom headers won't work" in text
        )


def test_prompt_scheme_note_https():
    text = _prompt(base_url="https://brain.example.com")
    assert "the endpoint is HTTPS" in text
    assert "plain HTTP" not in text


def test_prompt_scheme_note_http():
    text = _prompt(base_url="http://192.0.2.10:8300")
    assert "the endpoint is plain HTTP (not HTTPS)" in text
    assert text.count("the endpoint is HTTPS") == 0


def test_prompt_scheme_note_omitted_for_unrecognized_scheme():
    text = _prompt(base_url="ftp://weird.example.com")
    assert "the endpoint is HTTPS" not in text
    assert "plain HTTP" not in text
    # the header rationale still stands on its own, without a scheme claim
    assert "won't work). The response" in text
