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

    # --- Room file attachments (ADR-0012 storage core; app/attachments.py)
    # ---
    # A disk-usage investigation on the host running this app (ADR-0012
    # decision 6) measured a single 20 GB root filesystem at ~85% used,
    # ~2.9 GB free, shared by the OS, Docker images, the Postgres volume,
    # and any attachments -- neither container has a disk limit set, so an
    # unbounded upload path can exhaust the disk and stop Postgres writing,
    # taking the whole app down. Every bound below exists to prevent that,
    # not to be a generous quota; all five are read once at process startup
    # like every other env-sourced value in this module (a change requires a
    # restart) and fail loudly at construction when out of range, same
    # posture as `llm_call_timeout_secs` above.
    #
    # Max bytes for a single uploaded file. Default 10 MiB (ADR-0012
    # decision 6, replacing an earlier, unshipped 25 MB guess). Bounded
    # 1 KiB (a lower bound below which "file upload" stops being a
    # meaningful feature) to 100 MiB (comfortably above the default, but
    # still well inside the default global ceiling -- an operator raising
    # this is trusted to also raise the ceiling if they mean to store
    # bigger files routinely).
    attachment_max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    # Max number of attachment references a single room may accumulate.
    # Default 10 (ADR-0012 decision 6). Bounded 1-1000 -- a secondary guard
    # against one room consuming the shared global budget on its own; the
    # global ceiling below is the primary protection.
    attachment_max_files_per_room: int = Field(default=10, ge=1, le=1000)
    # Global ceiling on total attachment storage (summed across deduplicated
    # blobs, decision 2 -- NOT summed across room references, which would
    # double-count a deduped file). Default 500 MiB (decision 6, replacing
    # an earlier, unshipped 100 MB-per-room guess -- this is a single
    # server-wide total, not per-room). Bounded 1 MiB to 1 TiB: the lower
    # bound keeps the setting meaningful, the upper bound is a sanity cap
    # against a fat-fingered value, not tied to any one host's actual disk
    # size (this app may run on hosts far larger or smaller than the one the
    # ADR's 20 GB measurement was taken on).
    attachment_global_ceiling_bytes: int = Field(default=500 * 1024 * 1024, ge=1024 * 1024, le=1024**4)
    # Free-disk floor: refuse any upload that would leave less than this
    # much free space, checked immediately before every write
    # (app/attachments.py), and -- since the independent-review Fix 2 --
    # coordinated across concurrently in-flight uploads via a process-wide
    # reservation counter (app/attachments.py's `_reserve_upload_budget`/
    # `_release_upload_budget`) so N simultaneous uploads can no longer each
    # observe "enough headroom" at the same instant and collectively
    # overshoot it. Default 2 GiB (decision 6) -- deliberately set BELOW
    # the measured ~2.9 GB free on the reference host: a 3 GB floor would
    # reject every upload from day one on that host, making the feature
    # unusable as shipped. Bounded 100 MiB (a floor that low is barely a
    # floor at all, but still catches the worst case) to 1 TiB.
    #
    # This is a SECONDARY, LIVE guard, not the primary one: the global
    # ceiling above (`attachment_global_ceiling_bytes`) is THE primary
    # protection -- a hard cap enforced under the same `SELECT ... FOR
    # UPDATE` row-lock discipline as every other counter this module
    # checks (`AttachmentStorageStats`), so it can never be raced past
    # regardless of concurrency, full stop. This floor, by contrast, is a
    # live read of the ACTUAL filesystem (`shutil.disk_usage()`) at write
    # time: it catches disk pressure the ceiling has no way to see --
    # anything else on the host consuming space (logs, backups, the
    # database itself), which is exactly why it exists at all -- but it
    # remains fundamentally a snapshot-and-check against real-world state,
    # not a DB-enforced hard limit like the ceiling is.
    attachment_free_disk_floor_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=100 * 1024 * 1024, le=1024**4)
    # Grace period after a room closes before its (otherwise-unreferenced)
    # attachments become eligible for deletion (decision 5). Default 7 days
    # -- rooms auto-close on a timer, possibly overnight (ADR-0007), so
    # instant deletion at close would destroy files the owner meant to
    # keep. Bounded 0 (delete-on-close, for an operator who wants no grace
    # at all) to 365 days (a full year is already far beyond "grace" and
    # into "effectively permanent" -- an operator who wants that should
    # save the file to the Brain instead, decision 3).
    attachment_grace_period_days: int = Field(default=7, ge=0, le=365)
    # Filesystem directory attachment blobs are written under -- the mount
    # point of the `attachment_data` named volume this ADR adds to
    # docker-compose.yml (decision 2, same "one more named volume" pattern
    # `db_data` already uses). Not bounded/validated beyond being a
    # non-empty string: an operator running outside Docker may point this
    # anywhere writable.
    attachment_storage_dir: str = Field(default="/data/attachments", min_length=1)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
