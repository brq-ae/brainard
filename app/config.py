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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
