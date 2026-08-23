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
"""

import logging
import time

import httpx

from app.llm_config import EffectiveLlmConfig

logger = logging.getLogger(__name__)

TEST_TIMEOUT_SECS = 20.0
TEST_MAX_TOKENS = 8
TEST_PROMPT = "Reply with exactly: OK"


async def _post_chat_completion(base_url: str, model: str, api_key: str | None) -> httpx.Response:
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
    async with httpx.AsyncClient(timeout=TEST_TIMEOUT_SECS) as http_client:
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

    started = time.monotonic()
    try:
        response = await _post_chat_completion(effective.base_url, effective.model, effective.api_key)
    except httpx.ConnectTimeout:
        return _timeout_result()
    except httpx.TimeoutException:
        return _timeout_result()
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


def _timeout_result() -> dict:
    return {
        "ok": False,
        "detail": f"Connection timed out after {TEST_TIMEOUT_SECS:.0f}s.",
        "latency_ms": None,
        "model_echo": None,
    }


# --- Librarian judgment calls (ADR-0010 phase 2: the built-in engine) ---
#
# `chat_completion_json` is the ONE outbound call shape the built-in
# librarian ever makes (app/librarian_engine.py): a single-shot chat
# completion, no tool use, no follow-up turns (ADR-0010 decision 1). It
# returns the raw assistant text; parsing that text as JSON is the caller's
# job (deliberately -- a malformed response is an ordinary, expected outcome
# the engine must handle conservatively, not an exception path).

LIBRARIAN_CALL_TIMEOUT_SECS = 30.0
LIBRARIAN_DEFAULT_MAX_TOKENS = 900


class LlmCallError(Exception):
    """Raised by `chat_completion_json` for every transport/HTTP/shape
    failure (timeout, connection error, non-2xx status, unparseable/empty
    response body). Callers (app/librarian_engine.py) treat this as exactly
    one failed judgment call -- logged at WARNING with the exception type
    only, counted toward the run's consecutive-failure abort threshold --
    never a crash.
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
    timeout: float = LIBRARIAN_CALL_TIMEOUT_SECS,
) -> str:
    """One single-shot chat completion (system + user turn only, no history,
    no tools) against the effective provider. Returns the raw assistant
    message content as a string -- never parsed here. Raises `LlmCallError`
    for every failure mode; never leaks the api_key (placed only in the
    outbound Authorization header, same as `_post_chat_completion` above)
    and never logs the prompt or response content -- callers log counts and
    exception types only, per ADR-0010 phase 2's logging discipline.
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
    except httpx.HTTPError as exc:
        raise LlmCallError(f"transport error ({type(exc).__name__})") from exc

    if response.status_code >= 400:
        raise LlmCallError(f"provider returned HTTP {response.status_code}")

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmCallError("provider response did not contain a usable message") from exc

    if not isinstance(content, str) or not content.strip():
        raise LlmCallError("provider returned an empty completion")

    return content
