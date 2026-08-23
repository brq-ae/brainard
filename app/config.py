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

    # --- LLM provider config (ADR-0010 phase 1: pluggable librarian
    # runtimes, built-in LLM client) ---
    # Env override for the LLM provider config normally stored in the
    # `llm_configs` table (app/llm_config.py). When BOTH `llm_base_url` and
    # `llm_model` are set, they take precedence over whatever is stored in
    # the database and NOTHING is written there -- see
    # app/llm_config.py's `resolve_llm_config` and ADR-0010 decision 3.
    # Leaving both unset (the default) falls back to the current DB
    # version, if any. `llm_api_key` is optional either way -- Ollama and
    # other local endpoints need none.
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None

    # --- Built-in librarian engine (ADR-0010 phase 2) ---
    # Master on/off switch for the always-on librarian loop started in
    # app/main.py's lifespan (mirrors app/room_sweeper.py's always-on task).
    # Default on: the loop itself already no-ops cleanly per cycle when no
    # LLM provider is configured (app/librarian_engine.py's `run_librarian`),
    # so leaving this true is safe out of the box -- this flag exists for an
    # operator who wants to disable the feature outright (e.g. while
    # debugging, or a deployment that only ever uses the external-agent
    # runtime) without unsetting the LLM provider config.
    librarian_enabled: bool = True
    # Seconds between scheduled runs. Default: once daily (86400s) -- the
    # librarian is a low-urgency curation pass, not a real-time process;
    # matches the external-agent runtime's traditional "runs once per
    # night" cadence (scripts/librarian-prompt.md).
    librarian_interval_secs: int = 86400

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
