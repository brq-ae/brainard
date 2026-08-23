"""Application configuration, sourced entirely from environment variables."""

from functools import lru_cache

from pydantic import Field, field_validator
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

    # --- LLM call timeouts (real deployment bug: an owner-configured local
    # Ollama REASONING model -- one that emits a long chain-of-thought
    # before any content -- blew past the previously-hardcoded 30s timeout
    # on an ordinary room transcript; the UI just showed a confusing
    # "transport error (ReadTimeout)"). A hosted API (OpenAI, OpenRouter,
    # ...) typically answers in a second or two, but a local model on
    # modest hardware, or any reasoning model "thinking" before it writes
    # the actual JSON answer, can legitimately take minutes. Both settings
    # are read once at process startup like every other env-sourced value
    # in this module -- changing either requires a restart.
    #
    # Per-call timeout for the actual judgment calls this app makes: room
    # AI actions (app/room_ai.py) and the built-in librarian's duplicate/
    # fork/lesson judgment calls (app/librarian_engine.py). Default 180s --
    # generous enough for a local/reasoning model working through a long
    # transcript; still bounded so a genuinely unreachable/hung provider
    # fails within a few minutes rather than hanging the request forever.
    llm_call_timeout_secs: float = Field(default=180.0, ge=5.0, le=1800.0)
    # Timeout for the "Test connection" probe (/ui/llm, POST
    # /v1/llm-config/test, app/llm_client.py). The test prompt itself is
    # tiny, but a cold model load on a local provider (first request after
    # the process/model starts) can still take a while. Default 60s --
    # shorter than the call timeout since this is meant to be a quick
    # sanity check, but longer than the old hardcoded 20s.
    llm_test_timeout_secs: float = Field(default=60.0, ge=5.0, le=1800.0)

    # --- Librarian inline "Run now" timeout (app/routers/librarian.py) ---
    # Bounds the SYNCHRONOUS `POST /v1/librarian/run` request (the owner
    # UI's "Run now" button, app/routers/ui_librarian.py) -- the nightly
    # SCHEDULED run (app/librarian_engine.py's `run_librarian_loop`) has no
    # such wrapper at all and is unaffected by this setting either way.
    #
    # Optional: left unset (the default, `None`), the effective wrapper
    # timeout is DERIVED from the run's actual LLM-call budget rather than
    # a flat number -- see app/routers/librarian.py's
    # `effective_inline_run_timeout_secs` for the exact formula (floored at
    # the previous flat 600s default so it never gets shorter than before
    # this setting existed, capped at a sane ceiling). A run makes up to
    # `max_llm_calls` SEQUENTIAL judgment calls, each now allowed up to
    # `llm_call_timeout_secs` -- with that raised (e.g. for a local
    # reasoning model), a flat 600s wrapper became disproportionately
    # short: as few as ~4 slow-but-SUCCESSFUL calls could exhaust it even
    # though nothing was actually failing, so "Run now" would routinely
    # time out on exactly the workload LLM_CALL_TIMEOUT_SECS was raised to
    # support.
    #
    # Set explicitly to override the derivation outright (e.g. a firm SLA
    # on how long the owner is willing to let the button hang) -- bounded
    # 60-7200s (1 minute to 2 hours: long enough to be meaningful, short
    # enough that a mistyped value can't hang a synchronous browser request
    # indefinitely); out of range fails clearly at startup, same posture as
    # `llm_call_timeout_secs`/`llm_test_timeout_secs` above. An empty
    # string (docker-compose forwards this as `${VAR:-}`, i.e. "" when
    # unset in .env, same as every other optional var in this file) is
    # treated as unset/derive, not a parse failure.
    librarian_inline_run_timeout_secs: float | None = Field(default=None, ge=60.0, le=7200.0)

    @field_validator("librarian_inline_run_timeout_secs", mode="before")
    @classmethod
    def _blank_librarian_inline_run_timeout_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
