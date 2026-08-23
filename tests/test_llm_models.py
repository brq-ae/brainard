"""Model discovery -- POST /v1/llm-config/models (app/routers/llm_config.py).
Lets the owner discover what models a configured (or not-yet-saved)
provider actually has, via the standard OpenAI-compatible `GET
{base_url}/models` endpoint, rather than typing an exact model tag from
memory.

The outbound call is mocked throughout: every test monkeypatches
`app.llm_client._get_models` (the one call site `list_provider_models`
makes -- see tests/test_llm_client.py for that function's own unit
coverage of parsing/capping/error-message behavior). This file focuses on
the router: owner-only auth, form-supplied vs. saved-config resolution,
and the enveloped error shape.
"""

from datetime import UTC, datetime

import httpx
from ulid import ULID

import app.llm_client as llm_client_module
from app.models import LlmConfig, Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _owner_headers(db_session) -> dict:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _machine_headers(db_session) -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


async def _configure_provider(db_session, *, base_url="http://saved-provider.invalid/v1", api_key="saved-secret-key") -> None:
    db_session.add(
        LlmConfig(
            id=str(ULID()),
            version=1,
            base_url=base_url,
            model="saved-model",
            api_key=api_key,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()


def _install_get_models_stub(monkeypatch, handler) -> None:
    monkeypatch.setattr(llm_client_module, "_get_models", handler)


# --- happy path ---


async def test_models_happy_path_returns_sorted_ids(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(200, json={"data": [{"id": "zeta-model"}, {"id": "alpha-model"}]})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["models"] == ["alpha-model", "zeta-model"]
    assert data["count"] == 2
    assert data["truncated"] is False


async def test_models_empty_body_omitted_entirely_falls_back_to_saved_config(client, db_session, monkeypatch):
    """A POST with no body at all (not even `{}`) must still work -- the
    request model is entirely optional.
    """
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", headers=owner_headers)

    assert resp.status_code == 200, resp.json()
    assert resp.json()["models"] == ["m1"]


# --- form-supplied override vs. saved config ---


async def test_models_uses_form_supplied_base_url_and_key_over_saved_config(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session, base_url="http://saved.invalid/v1", api_key="saved-key")

    captured = {}

    async def fake_get_models(base_url, api_key, *, timeout):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return httpx.Response(200, json={"data": [{"id": "new-model"}]})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post(
        "/v1/llm-config/models",
        json={"base_url": "http://not-yet-saved.invalid/v1", "api_key": "new-key"},
        headers=owner_headers,
    )

    assert resp.status_code == 200, resp.json()
    assert captured["base_url"] == "http://not-yet-saved.invalid/v1"
    assert captured["api_key"] == "new-key"


async def test_models_form_supplied_base_url_without_key_does_not_leak_saved_key(client, db_session, monkeypatch):
    """Security-critical: an owner-typed base_url with a BLANK api_key must
    never silently reuse whatever key is stored for a different, already-
    saved provider -- see app/llm_config.py's `resolve_models_source`
    docstring.
    """
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session, base_url="http://saved.invalid/v1", api_key="saved-secret-key")

    captured = {}

    async def fake_get_models(base_url, api_key, *, timeout):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return httpx.Response(200, json={"data": []})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post(
        "/v1/llm-config/models", json={"base_url": "http://not-yet-saved.invalid/v1"}, headers=owner_headers
    )

    assert resp.status_code == 200, resp.json()
    assert captured["base_url"] == "http://not-yet-saved.invalid/v1"
    assert captured["api_key"] is None


async def test_models_falls_back_to_saved_config_when_base_url_omitted(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session, base_url="http://saved.invalid/v1", api_key="saved-secret-key")

    captured = {}

    async def fake_get_models(base_url, api_key, *, timeout):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return httpx.Response(200, json={"data": []})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={"api_key": None}, headers=owner_headers)

    assert resp.status_code == 200, resp.json()
    assert captured["base_url"] == "http://saved.invalid/v1"
    assert captured["api_key"] == "saved-secret-key"


async def test_models_no_provider_configured_and_none_given_clean_error(client, db_session):
    owner_headers = await _owner_headers(db_session)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_llm_provider_configured"


# --- owner-only ---


async def test_models_machine_token_forbidden(client, db_session):
    machine_headers = await _machine_headers(db_session)

    resp = await client.post("/v1/llm-config/models", json={}, headers=machine_headers)

    assert resp.status_code == 403


async def test_models_unauthenticated_rejected(client, db_session):
    resp = await client.post("/v1/llm-config/models", json={})

    assert resp.status_code in (401, 403)


# --- clean enveloped errors ---


async def test_models_connection_refused_clean_error(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        raise httpx.ConnectError("simulated")

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "llm_models_request_failed"
    assert "unreachable" in body["error"]["detail"].lower() or "refused" in body["error"]["detail"].lower()


async def test_models_timeout_clean_error(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        raise httpx.ReadTimeout("simulated")

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "llm_models_request_failed"
    assert "did not respond within" in body["error"]["detail"]


async def test_models_401_clean_error_key_never_leaked(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session, api_key="sk-supersecretvalue")

    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(401, json={"error": "invalid api key"})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "llm_models_auth_failed"
    assert "sk-supersecretvalue" not in resp.text


async def test_models_non_json_body_clean_error(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(200, content=b"<html>not json</html>")

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "llm_models_unsupported"
    assert "may not implement" in body["error"]["detail"]


async def test_models_missing_data_key_clean_error(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(200, json={"unexpected_shape": True})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "llm_models_unsupported"


async def test_models_1000_plus_models_truncation_disclosed(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)
    await _configure_provider(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(200, json={"data": [{"id": f"model-{i:05d}"} for i in range(1200)]})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post("/v1/llm-config/models", json={}, headers=owner_headers)

    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["truncated"] is True
    assert data["count"] == 500
    assert len(data["models"]) == 500


# --- invalid form-supplied base_url is still validated (header/URL-injection guard) ---


async def test_models_invalid_base_url_rejected(client, db_session):
    owner_headers = await _owner_headers(db_session)

    resp = await client.post("/v1/llm-config/models", json={"base_url": "not-a-url"}, headers=owner_headers)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_base_url"


# --- api_key never appears in the response body ---


async def test_models_api_key_never_in_success_response(client, db_session, monkeypatch):
    owner_headers = await _owner_headers(db_session)

    async def fake_get_models(base_url, api_key, *, timeout):
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    _install_get_models_stub(monkeypatch, fake_get_models)

    resp = await client.post(
        "/v1/llm-config/models",
        json={"base_url": "http://fake.invalid/v1", "api_key": "sk-topsecret999"},
        headers=owner_headers,
    )

    assert resp.status_code == 200, resp.json()
    assert "sk-topsecret999" not in resp.text
