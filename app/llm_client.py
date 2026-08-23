"""Outbound LLM connectivity test (ADR-0010 phase 1) -- a single minimal
OpenAI-compatible chat-completion request to the effective provider, used
only by `POST /v1/llm-config/test` and its UI equivalent
(app/routers/ui_llm.py) to confirm a configured base_url/model/api_key
actually work.

This is deliberately NOT the librarian's judgment call (phase 2, not built
yet) -- just a connectivity/credentials probe, one request, no retries
beyond httpx's own connection handling, and no agentic loop (ADR-0010
decision 1: the librarian itself is deterministic Python, not a tool-use
loop; this module doesn't even reach that far).

Never leaks the api_key: it is placed only in the outbound Authorization
header (never echoed into the response or a log line), and every failure
path below returns a clean, self-explaining message -- never a raw
exception/traceback -- to both the caller and the log.

Timeouts (both call sites in this module) are owner-configurable
(app/config.py's `llm_call_timeout_secs`/`llm_test_timeout_secs`, env
`LLM_CALL_TIMEOUT_SECS`/`LLM_TEST_TIMEOUT_SECS`) rather than hardcoded --
a real deployment hit a hardcoded 30s timeout with a local Ollama
REASONING model (one that emits a long chain-of-thought before any
content) and saw a confusing "transport error (ReadTimeout)" on an
ordinary transcript. Every timeout message below says plainly that the
provider simply didn't respond in time -- not that it's unreachable -- and
points at the setting to raise.
"""

import logging
import time

import httpx

from app.config import get_settings
from app.llm_config import EffectiveLlmConfig

logger = logging.getLogger(__name__)

# Generous enough that a reasoning model's chain-of-thought doesn't exhaust
# the completion budget before it ever reaches the literal "OK" this test
# prompt asks for -- a real deployment observed 696 completion tokens spent
# on internal reasoning for a comparably trivial prompt (see
# `chat_completion_json`'s empty-content handling below for the same
# failure mode on the judgment-call path). A tiny budget here doesn't save
# meaningful latency (the model still has to load/think either way) but
# does turn "connected fine, just verbose" into a spurious test failure.
TEST_MAX_TOKENS = 500
TEST_PROMPT = "Reply with exactly: OK"


async def _post_chat_completion(base_url: str, model: str, api_key: str | None, *, timeout: float) -> httpx.Response:
    """The one outbound call, factored out so tests can monkeypatch just
    this call (mirrors app/notify.py's `_send_ntfy` / app/notifications.py's
    `_insert_config` "factor out the one risky/mockable call" style).

    No Authorization header at all when no key is configured (rather than
    an empty-string Bearer) -- correct for Ollama and other local endpoints
    that reject or ignore the header differently; the test below asserts
    on this distinction directly.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": TEST_MAX_TOKENS,
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        return await http_client.post(url, json=payload, headers=headers)


async def test_llm_connection(effective: EffectiveLlmConfig) -> dict:
    """Returns a plain dict shaped like LlmConfigTestResponse:
    {"ok", "detail", "latency_ms", "model_echo"}. Never raises -- every
    exception (connect error, timeout, malformed response, anything else)
    is caught here and turned into a clean `ok=False` result.
    """
    if not effective.base_url or not effective.model:
        return {
            "ok": False,
            "detail": "No LLM provider configured -- set base_url and model first.",
            "latency_ms": None,
            "model_echo": None,
        }

    timeout = get_settings().llm_test_timeout_secs
    started = time.monotonic()
    try:
        response = await _post_chat_completion(effective.base_url, effective.model, effective.api_key, timeout=timeout)
    except httpx.TimeoutException:
        return _timeout_result(timeout)
    except httpx.ConnectError:
        return {
            "ok": False,
            "detail": "Connection refused or unreachable -- check base_url and that the provider is running.",
            "latency_ms": None,
            "model_echo": None,
        }
    except httpx.HTTPError as exc:
        # Any other httpx-level transport failure (bad URL scheme already
        # rejected earlier by validate_base_url, but e.g. TLS errors,
        # too-many-redirects, etc. can still surface here) -- log the
        # exception type only, never the request/response detail, which
        # could include the Authorization header.
        logger.warning("llm connectivity test transport error: %s", type(exc).__name__)
        return {
            "ok": False,
            "detail": f"Request failed ({type(exc).__name__}). Check base_url and network connectivity.",
            "latency_ms": None,
            "model_echo": None,
        }
    except Exception:
        # Belt-and-braces: never let an unexpected error surface as a raw
        # traceback to the owner -- log server-side (no key material is in
        # scope to leak here), return a clean enveloped-style message.
        logger.exception("llm connectivity test failed unexpectedly")
        return {
            "ok": False,
            "detail": "The connectivity test failed unexpectedly. Check the server logs for detail.",
            "latency_ms": None,
            "model_echo": None,
        }

    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code == 401 or response.status_code == 403:
        return {
            "ok": False,
            "detail": f"Authentication failed (HTTP {response.status_code}) -- check the API key.",
            "latency_ms": latency_ms,
            "model_echo": None,
        }
    if response.status_code == 404:
        return {
            "ok": False,
            "detail": "Not found (HTTP 404) -- check base_url and that the model name is correct.",
            "latency_ms": latency_ms,
            "model_echo": None,
        }
    if response.status_code >= 400:
        return {
            "ok": False,
            "detail": f"Provider returned HTTP {response.status_code}.",
            "latency_ms": latency_ms,
            "model_echo": None,
        }

    model_echo = None
    try:
        data = response.json()
        if isinstance(data, dict):
            echoed = data.get("model")
            if isinstance(echoed, str):
                model_echo = echoed
    except ValueError:
        pass

    return {"ok": True, "detail": "Connected successfully.", "latency_ms": latency_ms, "model_echo": model_echo}


def _timeout_result(timeout: float) -> dict:
    return {
        "ok": False,
        "detail": (
            f"The provider did not respond within {timeout:.0f}s -- it may still have been working, not "
            "unreachable. A local or reasoning model may need a longer LLM_TEST_TIMEOUT_SECS (or "
            "LLM_CALL_TIMEOUT_SECS for the actual room/librarian calls), or try a smaller/non-reasoning model."
        ),
        "latency_ms": None,
        "model_echo": None,
    }


# --- Model discovery (GET {base_url}/models) -- lets the owner pick a model
# from what the provider actually has installed, rather than typing an
# exact tag from memory (e.g. "gemma4:12b-it-q8_0"). Every OpenAI-compatible
# provider (Ollama, OpenAI, OpenRouter, DeepSeek, LM Studio, vLLM) exposes
# this the same way: `{"data": [{"id": "...", ...}, ...]}`. Used by
# `POST /v1/llm-config/models` (app/routers/llm_config.py) and its UI
# equivalent (app/routers/ui_llm.py's "Fetch models" button). Same posture
# as `test_llm_connection` above: one request, no retries, api_key never
# leaves the outbound Authorization header, and every failure path is a
# clean, self-explaining message -- never a raw traceback. ---

# A misbehaving or enormous provider catalog (some OpenRouter-style
# aggregators list hundreds of models) shouldn't turn into an unbounded
# response body -- capped, with `truncated` disclosed to the caller so the
# UI can say so rather than silently showing a partial list as complete.
MODELS_LIST_CAP = 500


class LlmModelsError(Exception):
    """Raised by `list_provider_models` for every failure mode (transport,
    auth, malformed/unexpected body). `code` is a short machine-readable
    reason the caller (app/routers/llm_config.py, app/routers/ui_llm.py)
    maps straight onto `ApiError`'s `code` -- the clean-message logic lives
    in exactly one place, same discipline as `LlmCallError` above. The
    message itself is always safe to display/log: never the api_key, never
    raw provider response bodies.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _get_models(base_url: str, api_key: str | None, *, timeout: float) -> httpx.Response:
    """The one outbound call, factored out so tests can monkeypatch just
    this call -- same style as `_post_chat_completion`/`_post_chat_messages`
    above. No Authorization header at all when no key is given, same
    reasoning as `_post_chat_completion`.
    """
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        return await http_client.get(url, headers=headers)


async def list_provider_models(base_url: str, api_key: str | None, *, timeout: float) -> tuple[list[str], bool]:
    """Returns `(model_ids, truncated)` -- `model_ids` sorted and
    deduplicated, capped at `MODELS_LIST_CAP`; `truncated` is True when the
    provider actually returned more than that. Raises `LlmModelsError` for
    every failure mode -- never a bare parse exception -- including a
    provider that doesn't implement this endpoint at all (some
    OpenAI-compatible servers don't), which is an ordinary, expected outcome
    here (the owner just types the model name instead), not a crash.
    """
    try:
        response = await _get_models(base_url, api_key, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LlmModelsError(
            "llm_models_request_failed",
            f"the provider did not respond within {timeout:.0f}s -- a local or reasoning model's endpoint may "
            "simply be slow to start; try again, or check LLM_TEST_TIMEOUT_SECS",
        ) from exc
    except httpx.ConnectError as exc:
        raise LlmModelsError(
            "llm_models_request_failed",
            "connection refused or unreachable -- check base_url and that the provider is running",
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("llm model discovery transport error: %s", type(exc).__name__)
        raise LlmModelsError(
            "llm_models_request_failed",
            f"request failed ({type(exc).__name__}) -- check base_url and network connectivity",
        ) from exc

    if response.status_code in (401, 403):
        raise LlmModelsError(
            "llm_models_auth_failed", f"authentication failed (HTTP {response.status_code}) -- check the API key"
        )
    if response.status_code >= 400:
        raise LlmModelsError(
            "llm_models_unsupported",
            f"the provider returned HTTP {response.status_code} for GET {base_url.rstrip('/')}/models -- it "
            "may not implement this endpoint; type the model name manually instead",
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise LlmModelsError(
            "llm_models_unsupported",
            f"the provider did not return valid JSON for GET {base_url.rstrip('/')}/models -- it may not "
            "implement this endpoint; type the model name manually instead",
        ) from exc

    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise LlmModelsError(
            "llm_models_unsupported",
            "the provider's response did not include the expected 'data' list (the standard OpenAI-compatible "
            f"shape) -- it may not implement GET {base_url.rstrip('/')}/models; type the model name manually "
            "instead",
        )

    ids = sorted({item["id"].strip() for item in data["data"] if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()})
    truncated = len(ids) > MODELS_LIST_CAP
    return ids[:MODELS_LIST_CAP], truncated


# --- Librarian judgment calls (ADR-0010 phase 2: the built-in engine) ---
#
# `chat_completion_json` is the ONE outbound call shape the built-in
# librarian ever makes (app/librarian_engine.py), and is also reused by
# app/room_ai.py's owner-triggered room AI actions: a single-shot chat
# completion, no tool use, no follow-up turns (ADR-0010 decision 1). It
# returns the raw assistant text; parsing that text as JSON is the caller's
# job (deliberately -- a malformed response is an ordinary, expected outcome
# the engine must handle conservatively, not an exception path).

# Headroom for a reasoning model's chain-of-thought PLUS the actual JSON
# answer -- a real deployment observed a local reasoning model spend 696
# completion tokens on internal reasoning before ever reaching content for
# a task far simpler than a real judgment call; a low budget here means the
# model can exhaust it mid-thought and return empty `content` (see the
# empty-completion handling below). Callers may still override via the
# `max_tokens` parameter for their own response shape.
LIBRARIAN_DEFAULT_MAX_TOKENS = 2000


class LlmCallError(Exception):
    """Raised by `chat_completion_json` for every transport/HTTP/shape
    failure (timeout, connection error, non-2xx status, unparseable/empty
    response body). Callers (app/librarian_engine.py, app/room_ai.py) treat
    this as exactly one failed judgment call -- logged at WARNING, counted
    toward the run's consecutive-failure abort threshold -- never a crash.
    The message itself is always safe to log/display: never the api_key,
    never the prompt or response content.
    """


async def _post_chat_messages(
    base_url: str, model: str, api_key: str | None, messages: list[dict], *, max_tokens: int, timeout: float
) -> httpx.Response:
    """The one outbound call, factored out so tests can monkeypatch just
    this call -- same "factor out the risky/mockable call" style as
    `_post_chat_completion` above.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        return await http_client.post(url, json=payload, headers=headers)


async def chat_completion_json(
    effective: EffectiveLlmConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = LIBRARIAN_DEFAULT_MAX_TOKENS,
    timeout: float,
) -> str:
    """One single-shot chat completion (system + user turn only, no history,
    no tools) against the effective provider. Returns the raw assistant
    message content as a string -- never parsed here. Raises `LlmCallError`
    for every failure mode; never leaks the api_key (placed only in the
    outbound Authorization header, same as `_post_chat_completion` above)
    and never logs the prompt or response content -- callers log counts and
    exception types only, per ADR-0010 phase 2's logging discipline.

    `timeout` is required (no module-level default) -- every call site
    threads through the owner-configured `llm_call_timeout_secs`
    (app/room_ai.py's `run_action`, app/librarian_engine.py's
    `LibrarianLimits.call_timeout_secs`) so there is exactly one place
    (app/config.py) that owns the effective value.
    """
    if not effective.base_url or not effective.model:
        raise LlmCallError("no LLM provider configured")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        response = await _post_chat_messages(
            effective.base_url,
            effective.model,
            effective.api_key,
            messages,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        # The specific bug this module exists to fix: httpx.TimeoutException
        # (ReadTimeout/ConnectTimeout/PoolTimeout/WriteTimeout) is an
        # httpx.HTTPError subclass, so it would otherwise fall into the
        # generic "transport error (ReadTimeout)" branch below -- which
        # reads exactly like a network fault even though the provider was
        # simply still working (a local/reasoning model can take minutes).
        # Caught first, and worded to say so plainly.
        raise LlmCallError(
            f"the provider did not respond within {timeout:.0f}s -- a local or reasoning model may need a "
            "longer LLM_CALL_TIMEOUT_SECS, or try a smaller/non-reasoning model"
        ) from exc
    except httpx.HTTPError as exc:
        raise LlmCallError(f"transport error ({type(exc).__name__})") from exc

    if response.status_code >= 400:
        raise LlmCallError(f"provider returned HTTP {response.status_code}")

    try:
        data = response.json()
        message = data["choices"][0]["message"]
        content = message["content"]
        finish_reason = data["choices"][0].get("finish_reason")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmCallError("provider response did not contain a usable message") from exc

    if not isinstance(content, str) or not content.strip():
        # Some OpenAI-compatible reasoning-model backends put chain-of-
        # thought in a separate `message.reasoning` field and can return an
        # EMPTY `content` when the completion-token budget is exhausted
        # mid-reasoning (observed in the wild: `content: ""`,
        # `finish_reason: "length"`). That combination gets its own,
        # actionable message rather than the generic empty-completion one
        # below -- `reasoning` is NEVER read as if it were the answer; the
        # `content` field is the contract, so this is still a hard failure,
        # just a diagnosable one.
        if finish_reason == "length":
            raise LlmCallError(
                "the model returned no content (it may have spent its output budget on internal reasoning; "
                "try a non-reasoning model or a larger max_tokens)"
            )
        raise LlmCallError("provider returned an empty completion")

    return content
