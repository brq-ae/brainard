"""app/llm_client.py -- the outbound LLM call functions. All httpx traffic
is mocked here (never a real network call); this suite is the unit-level
counterpart to tests/test_room_ai.py, tests/test_librarian_engine.py, and
tests/test_llm_config.py (which exercise the same module through their own
call sites/endpoints).

Focus: (1) the configured timeout actually reaches the outbound call, (2) a
provider timeout produces a self-explaining message ("the provider did not
respond in time, it may still be thinking") instead of the old "transport
error (ReadTimeout)" wording that reads like a network fault, and (3) a
reasoning-model response shape -- empty `content` with `finish_reason:
"length"` -- produces a clear, actionable error rather than a generic parse
failure or (worse) silently treating the `reasoning` field as the answer.
"""

import httpx
import pytest

import app.llm_client as llm_client_module
from app.config import get_settings
from app.llm_client import LlmCallError, LlmModelsError, chat_completion_json, list_provider_models
from app.llm_config import EffectiveLlmConfig

# NOT imported by name: `app.llm_client.test_llm_connection` is a normal
# function whose name happens to start with "test_" -- importing it
# directly would make pytest try to collect it as a test case (and fail,
# since its one parameter isn't a fixture). Called as
# `llm_client_module.test_llm_connection(...)` below instead.

EFFECTIVE = EffectiveLlmConfig(base_url="http://fake-provider.invalid/v1", model="fake-model", api_key=None, source="db")


def _response(status_code: int = 200, json_body: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=json_body if json_body is not None else {})


# --- chat_completion_json: the configured timeout/max_tokens actually reach the call ---


async def test_chat_completion_json_passes_through_timeout_and_max_tokens(monkeypatch):
    captured = {}

    async def fake_post_chat_messages(base_url, model, api_key, messages, *, max_tokens, timeout):
        captured["base_url"] = base_url
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        captured["timeout"] = timeout
        return _response(json_body={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    result = await chat_completion_json(EFFECTIVE, system_prompt="sys", user_prompt="user", max_tokens=1234, timeout=42.5)

    assert result == "ok"
    assert captured["timeout"] == 42.5
    assert captured["max_tokens"] == 1234
    assert captured["base_url"] == EFFECTIVE.base_url


# --- timeout -> self-explaining LlmCallError, not "transport error (ReadTimeout)" ---


@pytest.mark.parametrize("exc_cls", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout])
async def test_chat_completion_json_timeout_message_is_self_explaining(monkeypatch, exc_cls):
    async def fake_post_chat_messages(*args, **kwargs):
        raise exc_cls("simulated timeout")

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    with pytest.raises(LlmCallError) as exc_info:
        await chat_completion_json(EFFECTIVE, system_prompt="sys", user_prompt="user", timeout=17.0)

    message = str(exc_info.value)
    assert "did not respond within 17s" in message
    assert "LLM_CALL_TIMEOUT_SECS" in message
    assert "reasoning" in message.lower()
    assert exc_cls.__name__ not in message
    assert "transport error" not in message.lower()


async def test_chat_completion_json_non_timeout_transport_error_still_labeled(monkeypatch):
    """A non-timeout transport failure (e.g. connection refused) keeps the
    existing "transport error (ExceptionName)" wording -- only the timeout
    case is confusing/mislabeled, so only it gets the new message.
    """

    async def fake_post_chat_messages(*args, **kwargs):
        raise httpx.ConnectError("simulated connect error")

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    with pytest.raises(LlmCallError, match=r"transport error \(ConnectError\)"):
        await chat_completion_json(EFFECTIVE, system_prompt="sys", user_prompt="user", timeout=17.0)


# --- reasoning-model response shape: empty content + finish_reason "length" ---


async def test_empty_content_with_finish_reason_length_is_actionable_not_a_crash(monkeypatch):
    async def fake_post_chat_messages(*args, **kwargs):
        return _response(
            json_body={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "", "reasoning": "thinking forever without ever answering..."},
                        "finish_reason": "length",
                    }
                ]
            }
        )

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    with pytest.raises(LlmCallError) as exc_info:
        await chat_completion_json(EFFECTIVE, system_prompt="sys", user_prompt="user", timeout=30.0)

    message = str(exc_info.value)
    assert "no content" in message.lower()
    assert "reasoning" in message.lower()
    assert "max_tokens" in message.lower()
    # never leaks the actual chain-of-thought text into the error message
    assert "thinking forever" not in message


async def test_empty_content_without_length_finish_reason_gets_generic_empty_error(monkeypatch):
    """Distinguishes the specific reasoning-budget diagnosis from an
    ordinary empty completion (e.g. finish_reason "stop") -- the generic
    message is unchanged for that case.
    """

    async def fake_post_chat_messages(*args, **kwargs):
        return _response(json_body={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    with pytest.raises(LlmCallError, match="empty completion"):
        await chat_completion_json(EFFECTIVE, system_prompt="sys", user_prompt="user", timeout=30.0)


async def test_missing_content_key_still_raises_cleanly(monkeypatch):
    async def fake_post_chat_messages(*args, **kwargs):
        return _response(json_body={"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]})

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    with pytest.raises(LlmCallError):
        await chat_completion_json(EFFECTIVE, system_prompt="sys", user_prompt="user", timeout=30.0)


# --- content present AND a reasoning field -> parses normally, reasoning ignored ---


async def test_content_present_with_reasoning_field_parses_normally_reasoning_ignored(monkeypatch):
    async def fake_post_chat_messages(*args, **kwargs):
        return _response(
            json_body={
                "choices": [
                    {
                        "message": {"content": '{"summary": "the real answer"}', "reasoning": "internal chain of thought, never the contract"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    result = await chat_completion_json(EFFECTIVE, system_prompt="sys", user_prompt="user", timeout=30.0)

    assert result == '{"summary": "the real answer"}'


# --- no provider configured -> unchanged behavior ---


async def test_chat_completion_json_no_provider_configured_raises_before_any_call(monkeypatch):
    called = False

    async def fake_post_chat_messages(*args, **kwargs):
        nonlocal called
        called = True
        return _response()

    monkeypatch.setattr(llm_client_module, "_post_chat_messages", fake_post_chat_messages)

    unset = EffectiveLlmConfig(base_url=None, model=None, api_key=None, source=None)
    with pytest.raises(LlmCallError, match="no LLM provider configured"):
        await chat_completion_json(unset, system_prompt="sys", user_prompt="user", timeout=30.0)

    assert called is False


# --- test_llm_connection: configured test timeout + self-explaining message ---


async def test_test_llm_connection_uses_configured_test_timeout(monkeypatch):
    captured = {}

    async def fake_post_chat_completion(base_url, model, api_key, *, timeout):
        captured["timeout"] = timeout
        return _response(json_body={"model": "fake-model"})

    monkeypatch.setattr(llm_client_module, "_post_chat_completion", fake_post_chat_completion)

    result = await llm_client_module.test_llm_connection(EFFECTIVE)

    assert result["ok"] is True
    assert captured["timeout"] == get_settings().llm_test_timeout_secs


async def test_test_llm_connection_timeout_message_is_self_explaining(monkeypatch):
    async def fake_post_chat_completion(*args, **kwargs):
        raise httpx.ReadTimeout("simulated")

    monkeypatch.setattr(llm_client_module, "_post_chat_completion", fake_post_chat_completion)

    result = await llm_client_module.test_llm_connection(EFFECTIVE)

    assert result["ok"] is False
    assert "did not respond within" in result["detail"]
    assert "LLM_TEST_TIMEOUT_SECS" in result["detail"]
    assert "not unreachable" in result["detail"] or "still have been working" in result["detail"]
    assert "ReadTimeout" not in result["detail"]


# --- model discovery: GET {base_url}/models (app/llm_client.py's
# `list_provider_models`/`_get_models`/`LlmModelsError`) ---


async def test_get_models_no_key_omits_authorization_header(monkeypatch):
    """`_get_models` is the one outbound call -- unit-tested directly here
    (bypassing `list_provider_models`'s own mocking in the tests below) so
    the header-building behavior itself, not just what gets threaded into
    it, is verified: same discipline as tests/test_llm_config.py's
    Authorization-header assertions for the connectivity test.
    """
    captured = {}

    async def fake_get(self, url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    await llm_client_module._get_models("http://fake-provider.invalid/v1", None, timeout=5.0)

    assert "Authorization" not in captured["headers"]
    assert captured["url"] == "http://fake-provider.invalid/v1/models"


async def test_get_models_with_key_sets_bearer_header(monkeypatch):
    captured = {}

    async def fake_get(self, url, headers=None, **kwargs):
        captured["headers"] = headers
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    await llm_client_module._get_models("http://fake-provider.invalid/v1", "secret-key-123", timeout=5.0)

    assert captured["headers"]["Authorization"] == "Bearer secret-key-123"


async def test_list_provider_models_happy_path_returns_sorted_deduped_ids(monkeypatch):
    async def fake_get_models(base_url, api_key, *, timeout):
        return _response(json_body={"data": [{"id": "zeta"}, {"id": "alpha"}, {"id": "mid"}, {"id": "alpha"}]})

    monkeypatch.setattr(llm_client_module, "_get_models", fake_get_models)

    models, truncated = await list_provider_models("http://fake-provider.invalid/v1", None, timeout=5.0)

    assert models == ["alpha", "mid", "zeta"]
    assert truncated is False


async def test_list_provider_models_ignores_malformed_items(monkeypatch):
    """A provider's `data` list with junk entries (missing/blank/non-string
    `id`, non-dict items) must not crash -- those entries are simply
    skipped, not surfaced as an error, since the well-formed entries are
    still usable.
    """

    async def fake_get_models(base_url, api_key, *, timeout):
        return _response(
            json_body={"data": [{"id": "good-one"}, {"no_id": "x"}, {"id": ""}, {"id": 123}, "not-a-dict", None]}
        )

    monkeypatch.setattr(llm_client_module, "_get_models", fake_get_models)

    models, truncated = await list_provider_models("http://fake-provider.invalid/v1", None, timeout=5.0)

    assert models == ["good-one"]
    assert truncated is False


@pytest.mark.parametrize("exc_cls", [httpx.ConnectError, httpx.TimeoutException])
async def test_list_provider_models_transport_failure_is_clean_error(monkeypatch, exc_cls):
    async def fake_get_models(base_url, api_key, *, timeout):
        raise exc_cls("simulated")

    monkeypatch.setattr(llm_client_module, "_get_models", fake_get_models)

    with pytest.raises(LlmModelsError) as exc_info:
        await list_provider_models("http://fake-provider.invalid/v1", None, timeout=5.0)

    assert exc_info.value.code == "llm_models_request_failed"


async def test_list_provider_models_401_is_clean_auth_error(monkeypatch):
    async def fake_get_models(base_url, api_key, *, timeout):
        return _response(status_code=401, json_body={"error": "invalid key"})

    monkeypatch.setattr(llm_client_module, "_get_models", fake_get_models)

    with pytest.raises(LlmModelsError) as exc_info:
        await list_provider_models("http://fake-provider.invalid/v1", "bad-key", timeout=5.0)

    assert exc_info.value.code == "llm_models_auth_failed"
    assert "401" in str(exc_info.value)
    assert "bad-key" not in str(exc_info.value)


async def test_list_provider_models_non_json_body_is_clean_unsupported_error(monkeypatch):
    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(200, content=b"not json at all")

    monkeypatch.setattr(llm_client_module, "_get_models", fake_get_models)

    with pytest.raises(LlmModelsError) as exc_info:
        await list_provider_models("http://fake-provider.invalid/v1", None, timeout=5.0)

    assert exc_info.value.code == "llm_models_unsupported"
    assert "may not implement" in str(exc_info.value)


async def test_list_provider_models_missing_data_key_is_clean_unsupported_error(monkeypatch):
    async def fake_get_models(base_url, api_key, *, timeout):
        return _response(json_body={"unexpected": "shape"})

    monkeypatch.setattr(llm_client_module, "_get_models", fake_get_models)

    with pytest.raises(LlmModelsError) as exc_info:
        await list_provider_models("http://fake-provider.invalid/v1", None, timeout=5.0)

    assert exc_info.value.code == "llm_models_unsupported"


async def test_list_provider_models_huge_response_is_capped_and_truncation_disclosed(monkeypatch):
    async def fake_get_models(base_url, api_key, *, timeout):
        return _response(json_body={"data": [{"id": f"model-{i:05d}"} for i in range(1500)]})

    monkeypatch.setattr(llm_client_module, "_get_models", fake_get_models)

    models, truncated = await list_provider_models("http://fake-provider.invalid/v1", None, timeout=5.0)

    assert len(models) == llm_client_module.MODELS_LIST_CAP
    assert truncated is True
    assert models == sorted(models)
