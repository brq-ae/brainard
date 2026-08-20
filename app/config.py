"""Application configuration, sourced entirely from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Async SQLAlchemy URL, e.g. postgresql+asyncpg://user:pass@host:5432/dbname
    database_url: str

    # --- Phase 6 UI (app/ui_auth.py) ---
    # Signs the owner UI session cookie. If unset, a random secret is
    # generated once at process startup (app/ui_auth.py) -- deliberate
    # choice over hard-failing: a LAN single-owner deployment where losing
    # sessions on restart (everyone just logs back in with the same owner
    # token) is a acceptable trade against forcing a mandatory secret before
    # the UI even boots. Documented in .env.example; set it explicitly for
    # sessions that should survive a restart.
    ui_session_secret: str | None = None
    # LAN deployments are commonly plain http (contracts-v1.md doesn't
    # mandate TLS), so the cookie's Secure flag defaults off; set true
    # behind TLS.
    ui_cookie_secure: bool = False

    # --- Onboarding prompt generator (app/onboarding.py) ---
    # Optional override for the hub base URL embedded in generated
    # onboarding prompts -- for deployments reachable at a different
    # address than the one the owner's own browser used to mint a machine
    # (reverse proxy, port-forward, VPN). Unset (default): prompts use
    # `request.base_url`, the address the request actually arrived on, same
    # precedent as the pre-existing paste-line.
    hub_public_url: str | None = None

    # Optional DNS failsafe appended to generated prompts (app/onboarding.py)
    # -- the hub's direct LAN address (plain http, no reverse proxy, no DNS
    # involved), for agent machines whose DNS is a public resolver (e.g.
    # 8.8.8.8) that can't resolve an intranet hostname like the one
    # HUB_PUBLIC_URL (or the request's own base URL) might point at. Unset
    # (default): generated prompts carry no failsafe line, unchanged from
    # before this setting existed.
    hub_fallback_url: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
