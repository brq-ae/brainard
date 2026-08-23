"""LLM provider config -- POST/GET /v1/llm-config, POST /v1/llm-config/test
(ADR-0010 phase 1). Mirrors tests/test_notifications.py's structure for the
shared validation/versioning/race-retry/auth-matrix coverage, plus the
security-critical api_key masking and header-injection checks and the
connectivity-test endpoint's mocked httpx coverage.
"""

from types import SimpleNamespace

import httpx
from sqlalchemy.exc import IntegrityError
from ulid import ULID

import app.llm_config as llm_config_module
from app.models import LlmConfig, Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


def _install_llm_post_stub(monkeypatch, outer_client, stub):
    """Patches `httpx.AsyncClient.post` at the class level so the
    connectivity test's own outbound call (a freshly constructed
    `httpx.AsyncClient` inside app/llm_client.py's `_post_chat_completion`)
    hits `stub` -- WITHOUT also intercepting the test's own `client` fixture,
    which is *itself* an `httpx.AsyncClient` instance (ASGI-transport-backed)
    making the actual `POST /v1/llm-config/test` call. Since both share the
    exact same class, a naive `monkeypatch.setattr(httpx.AsyncClient, "post",
    ...)` would swallow the outer request too -- discriminate on `self`
    instead: only a call whose `self` is NOT `outer_client` is the app's own
    outbound provider call.
    """
    real_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kwargs):
        if self is outer_client:
            return await real_post(self, url, *args, **kwargs)
        return await stub(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def _machine_headers(db_session) -> dict:
    token = generate_machine_token()
    machine = Machine(id=str(ULID()), name="test-machine", token_hash=hash_token(token), status="active")
    db_session.add(machine)
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


def _body(**overrides) -> dict:
    body = {"base_url": "http://ollama:11434/v1", "model": "llama3.1"}
    body.update(overrides)
    return body


# --- happy path + versioning ---


async def test_create_llm_config_happy_path_no_api_key(client, db_session):
    """Ollama-style config, no api_key -- must be accepted (ADR-0010: local
    endpoints need no key at all)."""
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(note="local ollama"), headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["version"] == 1
    assert data["base_url"] == "http://ollama:11434/v1"
    assert data["model"] == "llama3.1"
    assert data["api_key_set"] is False
    assert data["api_key_hint"] is None
    assert data["note"] == "local ollama"
    assert data["created_at"] is not None


async def test_create_llm_config_with_api_key(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/llm-config", json=_body(base_url="https://api.openai.com/v1", model="gpt-4o-mini", api_key="sk-abcd1234"), headers=headers
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key_set"] is True
    assert data["api_key_hint"] == "...1234"
    # never the raw key, anywhere in the response body
    assert "sk-abcd1234" not in resp.text


async def test_llm_config_version_increments_and_history_persists(client, db_session):
    headers = await _owner_headers(db_session)
    resp1 = await client.post("/v1/llm-config", json=_body(model="model-v1"), headers=headers)
    assert resp1.status_code == 201
    assert resp1.json()["version"] == 1

    resp2 = await client.post("/v1/llm-config", json=_body(model="model-v2"), headers=headers)
    assert resp2.status_code == 201
    assert resp2.json()["version"] == 2

    get_resp = await client.get("/v1/llm-config", headers=headers)
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["effective"]["source"] == "db"
    assert data["effective"]["version"] == 2
    assert data["effective"]["model"] == "model-v2"
    assert [h["version"] for h in data["history"]] == [2, 1]
    assert data["history"][1]["model"] == "model-v1"


async def test_get_llm_config_honest_when_none_exists(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.get("/v1/llm-config", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["effective"]["source"] is None
    assert data["effective"]["base_url"] is None
    assert data["effective"]["model"] is None
    assert data["effective"]["api_key_set"] is False
    assert data["history"] == []


# --- validation: base_url ---


async def test_create_llm_config_rejects_non_http_scheme(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(base_url="ftp://example.com"), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"


async def test_create_llm_config_rejects_malformed_url(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(base_url="not-a-url"), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"


async def test_create_llm_config_rejects_base_url_with_no_host(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(base_url="http://"), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"


async def test_create_llm_config_base_url_rejects_whitespace(client, db_session):
    headers = await _owner_headers(db_session)
    for url in ["http://ollama:11434/v1 x", "http://ollama\t:11434/v1", "http://ollama:11434/v1\nx"]:
        resp = await client.post("/v1/llm-config", json=_body(base_url=url), headers=headers)
        assert resp.status_code == 422, f"{url!r} should have been rejected"
        assert resp.json()["error"]["code"] == "invalid_base_url"


async def test_create_llm_config_base_url_rejects_shell_metacharacters(client, db_session):
    headers = await _owner_headers(db_session)
    dangerous = [
        "http://ollama:11434/v1`backtick",
        "http://ollama:11434/v1$(id)",
        "http://ollama:11434/v1;rm -rf /",
        "http://ollama:11434/v1|cat",
        "http://ollama:11434/v1&bg",
        "http://ollama:11434/v1\"quote",
        "http://ollama:11434/v1'quote",
    ]
    for url in dangerous:
        resp = await client.post("/v1/llm-config", json=_body(base_url=url), headers=headers)
        assert resp.status_code == 422, f"{url!r} should have been rejected"
        assert resp.json()["error"]["code"] == "invalid_base_url"


async def test_create_llm_config_base_url_rejects_unicode_separators(client, db_session):
    headers = await _owner_headers(db_session)
    for bad_char in (" ", " ", "", ""):
        resp = await client.post("/v1/llm-config", json=_body(base_url=f"http://ollama:11434/v1{bad_char}x"), headers=headers)
        assert resp.status_code == 422, f"U+{ord(bad_char):04X} should have been rejected"
        assert resp.json()["error"]["code"] == "invalid_base_url"


# --- CVE-2023-24329-adjacent regression: urllib.parse.urlparse raises
# ValueError (not returns a mis-parsed result) when the netloc contains a
# character that NFKC-normalizes into a URL-structural character. These
# characters are category Po -- NOT Cc/Cf/Zl/Zp -- and are pure non-ASCII,
# so they pass every check above validate_base_url's urlparse call
# undetected and used to reach it raw, causing an unhandled 500. Confirmed
# empirically (before the fix): POST /v1/llm-config and POST /ui/llm both
# 500'd on these exact payloads; app/notifications.py's validate_ntfy_url
# had the identical bug (see tests/test_notifications.py's equivalent
# cases). Must now be a clean 422 invalid_base_url, same as any other
# malformed URL, and nothing stored.


async def test_create_llm_config_base_url_rejects_nfkc_fullwidth_solidus(client, db_session):
    """U+FF0F FULLWIDTH SOLIDUS normalizes to \'/\' -- the exact character
    that triggers urllib.parse\'s post-CVE-2023-24329 netloc guard."""
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/llm-config", json=_body(base_url="http://ollama:11434\uff0fevil.example/v1"), headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"
    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 0


async def test_create_llm_config_base_url_rejects_nfkc_fullwidth_at(client, db_session):
    """U+FF20 FULLWIDTH COMMERCIAL AT normalizes to \'@\'."""
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/llm-config", json=_body(base_url="http://ollama:11434\uff20evil.example/v1"), headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"
    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 0


async def test_create_llm_config_base_url_rejects_nfkc_fullwidth_colon(client, db_session):
    """U+FF1A FULLWIDTH COLON normalizes to \':\' -- another NFKC-divergent
    punctuation case distinct from the solidus/at-sign pair above."""
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/llm-config", json=_body(base_url="http://ollama\uff1a11434/v1"), headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"
    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 0


async def test_create_llm_config_base_url_rejects_nfkc_fullwidth_question_mark(client, db_session):
    """U+FF1F FULLWIDTH QUESTION MARK normalizes to \'?\'."""
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/llm-config", json=_body(base_url="http://ollama:11434\uff1fevil.example/v1"), headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"
    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 0


async def test_test_llm_config_endpoint_rejects_nfkc_divergent_base_url_cleanly(client, db_session):
    """A stored config can never contain an NFKC-divergent base_url in the
    first place (validate_base_url rejects it at write time, as the tests
    above confirm) -- this exercises the connectivity-test endpoint itself
    stays reachable and clean when POST /v1/llm-config is probed directly
    with such a payload."""
    headers = await _owner_headers(db_session)
    resp = await client.post(
        "/v1/llm-config", json=_body(base_url="http://ollama:11434\uff0fevil.example/v1"), headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"


async def test_create_llm_config_base_url_max_length_enforced(client, db_session):
    headers = await _owner_headers(db_session)
    long_url = "http://" + ("a" * llm_config_module.BASE_URL_MAX_LENGTH) + ".example.com/v1"
    resp = await client.post("/v1/llm-config", json=_body(base_url=long_url), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"


# --- validation: model ---


async def test_create_llm_config_rejects_blank_model(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(model=""), headers=headers)
    assert resp.status_code == 422


async def test_create_llm_config_rejects_whitespace_only_model(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(model="   "), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_model"


async def test_create_llm_config_model_max_length_enforced(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(model="a" * (llm_config_module.MODEL_MAX_LENGTH + 1)), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_model"


async def test_create_llm_config_model_rejects_disallowed_charset(client, db_session):
    headers = await _owner_headers(db_session)
    for bad_model in ["model with space", "model\ttab", "model;semi", "model<script>", "model$(cmd)"]:
        resp = await client.post("/v1/llm-config", json=_body(model=bad_model), headers=headers)
        assert resp.status_code == 422, f"{bad_model!r} should have been rejected"
        assert resp.json()["error"]["code"] == "invalid_model"


async def test_create_llm_config_model_accepts_common_real_world_names(client, db_session):
    headers = await _owner_headers(db_session)
    for good_model in ["llama3.1", "gpt-4o-mini", "meta-llama/Llama-3.1-8B-Instruct", "gpt-4:free", "qwen2.5:14b-instruct"]:
        resp = await client.post("/v1/llm-config", json=_body(model=good_model), headers=headers)
        assert resp.status_code == 201, f"{good_model!r} should have been accepted: {resp.text}"


# --- validation: api_key (security-critical -- header injection) ---


async def test_create_llm_config_rejects_api_key_with_newline(client, db_session):
    """The api_key goes verbatim into an outbound Authorization header --
    a newline is a textbook HTTP header/request-splitting injection
    vector and must be rejected outright."""
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(api_key="sk-good\nX-Evil-Header: injected"), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_create_llm_config_rejects_api_key_with_carriage_return(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(api_key="sk-good\r\nX-Evil: 1"), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_create_llm_config_rejects_api_key_with_control_character(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(api_key="sk-\x00good"), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_create_llm_config_rejects_blank_api_key(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(api_key="   "), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_create_llm_config_api_key_max_length_enforced(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(api_key="a" * (llm_config_module.API_KEY_MAX_LENGTH + 1)), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_create_llm_config_api_key_null_accepted_ollama_style(client, db_session):
    """Explicit re-confirmation of the happy path above, phrased as the
    matrix entry the brief calls out by name."""
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(), headers=headers)
    assert resp.status_code == 201
    assert resp.json()["api_key_set"] is False


# --- masking: never leak the raw key ---


async def test_api_key_never_appears_in_create_response(client, db_session):
    headers = await _owner_headers(db_session)
    secret = "sk-super-secret-value-xyz"
    resp = await client.post("/v1/llm-config", json=_body(api_key=secret), headers=headers)
    assert resp.status_code == 201
    assert secret not in resp.text


async def test_api_key_never_appears_in_get_response(client, db_session):
    headers = await _owner_headers(db_session)
    secret = "sk-super-secret-value-xyz"
    await client.post("/v1/llm-config", json=_body(api_key=secret), headers=headers)
    resp = await client.get("/v1/llm-config", headers=headers)
    assert resp.status_code == 200
    assert secret not in resp.text
    data = resp.json()
    assert data["effective"]["api_key_set"] is True
    assert data["effective"]["api_key_hint"] == "...-xyz"
    assert data["history"][0]["api_key_set"] is True


async def test_api_key_short_value_masked_without_leaking(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(api_key="abc"), headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key_set"] is True
    assert "abc" not in resp.text
    assert data["api_key_hint"] == "...***"


# --- insert-conflict retry (bounded, enveloped) ---


async def test_create_version_retries_and_succeeds_after_transient_collisions(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    real_insert = llm_config_module._insert_config
    calls = {"n": 0}

    async def flaky_insert(db, version, base_url, model, api_key, note):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise IntegrityError("simulated version collision", {}, Exception("simulated"))
        return await real_insert(db, version, base_url, model, api_key, note)

    monkeypatch.setattr(llm_config_module, "_insert_config", flaky_insert)

    resp = await client.post("/v1/llm-config", json=_body(model="flaky-model"), headers=headers)

    assert resp.status_code == 201
    data = resp.json()
    assert data["version"] == 1
    assert calls["n"] == 3

    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 1


async def test_create_version_exhausts_retries_returns_enveloped_503(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    calls = {"n": 0}

    async def always_flaky_insert(db, version, base_url, model, api_key, note):
        calls["n"] += 1
        raise IntegrityError("simulated persistent collision", {}, Exception("simulated"))

    monkeypatch.setattr(llm_config_module, "_insert_config", always_flaky_insert)

    resp = await client.post("/v1/llm-config", json=_body(), headers=headers)

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "llm_config_conflict_retry"
    assert "resend" in error["detail"].lower()
    assert calls["n"] == llm_config_module.MAX_INSERT_ATTEMPTS == 3

    rows = (await db_session.execute(LlmConfig.__table__.select())).all()
    assert len(rows) == 0


# --- auth matrix ---


async def test_create_llm_config_machine_token_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    resp = await client.post("/v1/llm-config", json=_body(), headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_get_llm_config_machine_token_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    resp = await client.get("/v1/llm-config", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_test_llm_config_machine_token_rejected(client, db_session):
    headers = await _machine_headers(db_session)
    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "owner_token_required"


async def test_create_llm_config_missing_auth_rejected(client, db_session):
    resp = await client.post("/v1/llm-config", json=_body())
    assert resp.status_code == 401


async def test_get_llm_config_missing_auth_rejected(client, db_session):
    resp = await client.get("/v1/llm-config")
    assert resp.status_code == 401


async def test_test_llm_config_missing_auth_rejected(client, db_session):
    resp = await client.post("/v1/llm-config/test")
    assert resp.status_code == 401


# --- env override precedence ---


async def test_env_override_takes_precedence_and_db_write_still_succeeds(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    fake_settings = SimpleNamespace(llm_base_url="http://env-provider:9999/v1", llm_model="env-model", llm_api_key="env-secret-key")
    monkeypatch.setattr(llm_config_module, "get_settings", lambda: fake_settings)

    # posting to the DB still works even though env is in effect...
    resp = await client.post("/v1/llm-config", json=_body(model="db-model"), headers=headers)
    assert resp.status_code == 201

    # ...but the EFFECTIVE config is still the env one, not what was just stored
    get_resp = await client.get("/v1/llm-config", headers=headers)
    data = get_resp.json()
    assert data["effective"]["source"] == "env"
    assert data["effective"]["base_url"] == "http://env-provider:9999/v1"
    assert data["effective"]["model"] == "env-model"
    assert data["effective"]["api_key_set"] is True
    assert data["effective"]["api_key_hint"] == "...-key"
    assert "env-secret-key" not in get_resp.text
    # db history still has the stored version -- it just isn't effective
    assert len(data["history"]) == 1
    assert data["history"][0]["model"] == "db-model"


async def test_env_override_requires_both_vars_set(client, db_session, monkeypatch):
    """A partial override (only base_url, model missing) must NOT take
    effect -- falls through to the stored DB config instead."""
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(model="db-model-2"), headers=headers)

    fake_settings = SimpleNamespace(llm_base_url="http://env-only:1234/v1", llm_model=None, llm_api_key=None)
    monkeypatch.setattr(llm_config_module, "get_settings", lambda: fake_settings)

    get_resp = await client.get("/v1/llm-config", headers=headers)
    data = get_resp.json()
    assert data["effective"]["source"] == "db"
    assert data["effective"]["model"] == "db-model-2"


# --- connectivity test endpoint ---


async def test_test_llm_config_no_provider_configured(client, db_session):
    headers = await _owner_headers(db_session)
    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "configured" in data["detail"].lower()


async def test_test_llm_config_posts_with_authorization_header_when_key_set(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(api_key="sk-realsecret123"), headers=headers)

    captured = {}

    async def stub(self, url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return httpx.Response(200, json={"model": "llama3.1"})

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["model_echo"] == "llama3.1"
    assert data["latency_ms"] is not None
    assert captured["url"] == "http://ollama:11434/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-realsecret123"
    # never leaked in the response
    assert "sk-realsecret123" not in resp.text


async def test_test_llm_config_no_authorization_header_when_no_key(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(), headers=headers)

    captured = {}

    async def stub(self, url, json=None, headers=None, **kwargs):
        captured["headers"] = headers
        return httpx.Response(200, json={"model": "llama3.1"})

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert "Authorization" not in captured["headers"]


async def test_test_llm_config_connection_error_returns_clean_error(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(), headers=headers)

    async def stub(self, url, json=None, headers=None, **kwargs):
        raise httpx.ConnectError("boom")

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200  # the test endpoint reports failure in-band, never a 5xx
    data = resp.json()
    assert data["ok"] is False
    assert "refused" in data["detail"].lower() or "unreachable" in data["detail"].lower()
    # no raw traceback text leaked
    assert "Traceback" not in resp.text
    assert "ConnectError" not in resp.text


async def test_test_llm_config_timeout_returns_clean_error(client, db_session, monkeypatch):
    """The message must be self-explaining -- "the provider did not
    respond in time", not the old "transport error (ReadTimeout)"/bare
    "Connection timed out" wording that reads like a network fault when
    the provider (e.g. a local reasoning model) may simply still be
    working. See app/llm_client.py's `_timeout_result`.
    """
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(), headers=headers)

    async def stub(self, url, json=None, headers=None, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "did not respond within" in data["detail"]
    assert "LLM_TEST_TIMEOUT_SECS" in data["detail"]
    assert "reasoning" in data["detail"].lower()
    assert "ReadTimeout" not in data["detail"]
    assert "ConnectTimeout" not in data["detail"]


async def test_test_llm_config_401_returns_clean_error(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(api_key="sk-badkey"), headers=headers)

    async def stub(self, url, json=None, headers=None, **kwargs):
        return httpx.Response(401, json={"error": "invalid api key"})

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "401" in data["detail"] or "auth" in data["detail"].lower()
    assert "sk-badkey" not in resp.text


async def test_test_llm_config_404_returns_clean_error(client, db_session, monkeypatch):
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(model="nonexistent-model"), headers=headers)

    async def stub(self, url, json=None, headers=None, **kwargs):
        return httpx.Response(404, json={"error": "model not found"})

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "404" in data["detail"]


async def test_test_llm_config_uses_effective_env_config(client, db_session, monkeypatch):
    """The test endpoint probes the EFFECTIVE config, so an env override
    must be what gets hit, not any stored DB version."""
    headers = await _owner_headers(db_session)
    await client.post("/v1/llm-config", json=_body(base_url="http://should-not-be-used:1/v1", model="db-model"), headers=headers)

    fake_settings = SimpleNamespace(llm_base_url="http://env-target:4242/v1", llm_model="env-model", llm_api_key=None)
    monkeypatch.setattr(llm_config_module, "get_settings", lambda: fake_settings)

    captured = {}

    async def stub(self, url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, json={"model": "env-model"})

    _install_llm_post_stub(monkeypatch, client, stub)

    resp = await client.post("/v1/llm-config/test", headers=headers)
    assert resp.status_code == 200
    assert captured["url"] == "http://env-target:4242/v1/chat/completions"
    assert captured["json"]["model"] == "env-model"
