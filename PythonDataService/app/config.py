"""Application configuration loaded from environment variables"""

import re
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Compose's own service-name shape: lowercase alphanumerics plus `_`, `.` and
# `-`, starting with an alphanumeric. Anything else is a deployment mistake,
# not a service this repo could restart.
_COMPOSE_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class FleetSettings(BaseSettings):
    """Fleet role and presence settings (ADR 0062, delivery A2).

    ``FLEET_ROLE`` names this process's composition explicitly — the
    ``combined`` default is the legacy/development compatibility posture
    only, and a deployment that runs the fleet names its roles (ADR 0062
    addendum). Once a volume is fleet-enrolled, a missing marker or fleet
    configuration in a non-combined role is a startup refusal, never a
    silent fallback to unfenced legacy authority.
    """

    model_config = SettingsConfigDict(
        env_prefix="FLEET_",
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    ROLE: Literal["combined", "fleet_coordinator", "clerk_agent"] = "combined"
    # The coordinator's own control volume root (registry lives under
    # <control_dir>/fleet/registry.db). Required for fleet_coordinator and
    # for a clerk_agent using local presence (single-host) instead of the
    # internal HTTP surface.
    CONTROL_DIR: Path | None = None
    # The clerk agent's identities: the opaque clerk it serves, its durable
    # worker key (presented at registration; never a transport credential),
    # and the deployment-approved endpoint reference it cites.
    CLERK_ID: str | None = None
    WORKER_KEY: str | None = None
    AGENT_ENDPOINT_REF: str | None = None
    # The internal coordinator base URL for remote presence. When set, the
    # agent registers over HTTP; when unset with a control dir, presence is
    # local (one process, one host).
    COORDINATOR_URL: str | None = None
    # The agent→coordinator transport token (env-only, minted by the host
    # ceremony; the coordinator holds the same value per clerk).
    AGENT_SERVICE_TOKEN: str | None = None
    # The coordinator→agent transport token this process accepts on its
    # served surface (env-only; the coordinator presents the same per-clerk
    # value when forwarding). The second minted direction of the same
    # provisioning ceremony — never stored, never logged.
    COORDINATOR_SERVICE_TOKEN: str | None = None
    # The coordinator's per-clerk coordinator-token mapping, as JSON text —
    # ``{"clrk_…": "svct_…"}`` (coordinator role only). Env-only credential
    # material; never stored in a registry and never logged.
    COORDINATOR_SERVICE_TOKENS_JSON: str = ""
    # The coordinator's per-clerk agent token mapping, as JSON text —
    # ``{"clrk_…": "svct_…"}``. Env-only credential material; never stored
    # in a registry and never logged.
    AGENT_SERVICE_TOKENS_JSON: str = ""
    # The deployment namespace qualifying volume roots (audit 2026-09-13,
    # finding 4).
    DEPLOYMENT_NAMESPACE: str = "host:local"
    # Heartbeat cadence; the registry's staleness window is 30 s.
    HEARTBEAT_INTERVAL_S: float = 10.0
    # A fleet clerk agent is explicitly sized by the deployment.  Zero keeps
    # the middleware disabled for the legacy/combined posture; a clerk-agent
    # process must supply positive values for both pools at startup.
    MAX_INFLIGHT_REQUESTS: int = 0
    MAX_INFLIGHT_STREAMS: int = 0
    # Requests may wait for a request or stream slot only within this bounded
    # queue and deadline. A zero queue limit refuses immediately.
    REQUEST_QUEUE_LIMIT: int = 0
    REQUEST_QUEUE_TIMEOUT_MS: int = 0
    # The compose service name of THIS worker process, declared by the
    # deployment beside the service it names. Its only use is authoring the
    # operator's restart command on the Alpaca desk: nothing the desk holds
    # maps a lane to a compose service, and the coordinator's service name
    # restarts the wrong process. Never an endpoint, never a credential, and
    # never projected into the public fleet directory (ADR 0062 §3).
    WORKER_SERVICE: str | None = None

    @field_validator("WORKER_SERVICE")
    @classmethod
    def _validate_worker_service(cls, value: str | None) -> str | None:
        """Fail at boot on a declaration no compose service could answer to.

        The value is pasted verbatim into a command an operator runs, so a
        malformed declaration must stop the process rather than reach a copy
        button. An empty value is Compose writing an unset ``${VAR}`` through
        — that is "undeclared", not a service named "".
        """
        if value is None or value == "":
            return None
        if not _COMPOSE_SERVICE_NAME.fullmatch(value):
            raise ValueError(
                f"FLEET_WORKER_SERVICE={value!r} is not a compose service name "
                "(lowercase alphanumerics, '_', '.' and '-', starting alphanumeric)."
            )
        return value


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Pydantic-v2 settings: ``extra="ignore"`` so a local ``.env`` with
    project-specific extras (test fixtures, optional vendor tokens,
    one-off experiments) does not break ``Settings()`` construction.
    The class-based ``Config`` previously rejected unknown keys with a
    ``ValidationError`` at module import; that broke ``pytest``
    collection any time a developer dropped an experimental key into
    ``.env``. ``extra="ignore"`` is the standard Pydantic-settings
    convention for application config models where the env file is
    operator-edited.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    # Polygon API
    POLYGON_API_KEY: str
    # Polygon's paid plans (Starter / Developer / Advanced / Business) have
    # no per-minute cap, so the throttle is off by default. Only the free
    # Basic tier is 5/min — set this to 5 if you're on Basic. See
    # docs/references/polygon-throttle.md for the full plan table.
    POLYGON_RATE_LIMIT_PER_MIN: int = 0

    # FRED API (for dynamic risk-free rate)
    FRED_API_KEY: str = ""

    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Git SHA of the code this container is running, surfaced on /health so an
    # operator can confirm the data plane matches master. The container has no
    # .git mount, so a live `git rev-parse` won't work here — this is sourced
    # from the GIT_COMMIT_SHA env/build-arg ("" if unset). The host daemon
    # (which executes live sessions) computes its own SHA live on /health.
    GIT_COMMIT_SHA: str = ""

    # CORS (comma-separated string)
    ALLOWED_ORIGINS: str = "http://backend:8080,http://localhost:5000,http://localhost:4200"

    def get_allowed_origins(self) -> list[str]:
        """Parse ALLOWED_ORIGINS into a list"""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",")]

    # Local control-plane guard. Mutating broker-control routes fail closed unless
    # this shared secret is configured or a local-dev operator explicitly opts out.
    DATA_PLANE_CONTROL_SECRET: str = ""
    DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL: bool = False
    # Broker-v2 panel operator identity (spec §14, interim posture). Control
    # mutations authenticate via DATA_PLANE_CONTROL_SECRET; the server attaches
    # THIS configured identity to journaled actions. Operator identity is never
    # a request field — no free-text identity input anywhere in the UI.
    PANEL_OPERATOR_IDENTITY: str = "operator"
    # Manual SQLite custody remains disabled until the paper qualification
    # ceremony supplies a release receipt. Routes expose the authored reason;
    # this flag alone never authorizes a live account.
    ALPACA_SQLITE_MANUAL_TRADING_ENABLED: bool = False
    # Account-level half of the two-key carryover permission. A deployment
    # must also opt in explicitly; the default remains flat-only Resume.
    ALPACA_PAPER_CARRYOVER_ENABLED: bool = False
    # Dev-only broker fault-injection seam (PRD #1354). Off by default; the seam
    # ALSO fails closed unless the Alpaca posture is paper. Never enable in a
    # live/production path — it exists to rehearse reject/throttle/conflict/
    # timeout/redelivery/halt scenarios against the real adapter+consumer without
    # ever placing an abnormal order.
    ALPACA_FAULT_INJECTION_ENABLED: bool = False
    TRUSTED_HOSTS: str = (
        "localhost,127.0.0.1,test,testserver,python-service,backend,"
        "host.containers.internal,host.docker.internal"
    )

    def get_trusted_hosts(self) -> list[str]:
        """Parse TRUSTED_HOSTS into a list for TrustedHostMiddleware."""
        return [host.strip() for host in self.TRUSTED_HOSTS.split(",") if host.strip()]

    # Data sanitization settings
    MAX_NULL_PERCENTAGE: float = 0.1  # 10% max nulls allowed
    REMOVE_DUPLICATES: bool = True
    FILL_METHOD: str = "ffill"  # forward fill for time series

    # .NET backend URL for study persistence
    BACKEND_URL: str = "http://localhost:5000"

    # Rate limiting (optional)
    MAX_REQUESTS_PER_MINUTE: int = 100

    # Data lake. Default-ON since #1839: the lake is the market-data
    # authority for historical bars in both adjustment modes (ADR 0049),
    # and the engines, the LEAN sidecar and the chart split-read all
    # resolve through it.
    #
    # What "on" does NOT mean, because the blast radius of this default is
    # the thing worth stating next to it: since #1866, each price-adjustment
    # mode resolves to its own root under the lake
    # (``path_policy.resolve_lake_root``), so an adjusted-bars request is
    # served by the lake too -- it is not carved out. Turning this off
    # remains a complete rollback: every seam asks one predicate and every
    # one of them falls back to the path it used before, demonstrated by
    # tests/engine/test_policy_store.py::test_turning_the_flag_off_returns_a_reader_to_the_policy_bars.
    # One cost, not a correctness gap: bars fetched while the flag was on
    # landed only in the lake, so a rollback re-fetches those windows from
    # Polygon on first use. The policy cache is never stale, only behind.
    #
    # postgres://user:pass@host:5432/dbname — required. The lake is the sole
    # market-data store (#1893 retired the policy store and the
    # DATA_LAKE_ENABLED flag that used to select between them), so a
    # deployment without POSTGRES_URL has no market data at all.
    POSTGRES_URL: str = ""
    # Rebuildable read model over canonical lifecycle/account artifacts.
    # Requires POSTGRES_URL when enabled; files remain canonical when disabled
    # or unavailable.
    # Clerk-native operator transaction history. This is deliberately a
    # separate read model from lifecycle projection tables and can fail
    # without changing Clerk acknowledgement durability.
    CLERK_TRANSACTION_PROJECTION_ENABLED: bool = False
    # Issue #1735 step 3. Off, a strategy-wiring digest that no longer matches
    # its golden-qualification receipt is reported and Start/Resume proceed;
    # on, it fails closed like any other build drift. Deliberately default-off:
    # the coverage is new, and every existing bot would otherwise be blocked by
    # a mismatch nobody has had a chance to re-qualify yet. Drift in the
    # *already-covered* artifacts keeps blocking either way -- this toggle
    # governs only the newly-added wiring half.
    SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED: bool = False
    # Data lake writer root (Slice 1b). Container-side path of the RW mount.
    # The writer creates lake/ and staging/ subdirectories under this path.
    # Must be on a single filesystem so POSIX atomic rename(2) is valid.
    LEAN_DATA_WRITE_ROOT: str = "/lean-data-writer"
    # Root identity (#1876, PR A of #1861): the physical lake root this
    # deployment is configured to treat as active. Empty means "the legacy
    # single root" — active_root_id() below falls back to LEGACY_ROOT_ID,
    # the same UUID the schema migration backfilled every pre-#1876 catalog
    # row with, so an upgrade needs no operator action to keep writing/
    # reading the same rows it always has.
    DATA_LAKE_ROOT_ID: str = ""
    # LEAN sidecar launcher (Slice 1c Phase 0 metadata extraction).
    # The launcher is a host process with podman access; the data-plane
    # container calls it via HTTP to extract market-hours-database.json
    # and symbol-properties-database.csv from the pinned LEAN image.
    # When running inside compose on Windows/WSL2, set to
    # http://host.containers.internal:8090. See PythonDataService/CLAUDE.md.
    LEAN_LAUNCHER_URL: str = "http://127.0.0.1:8090"
    LEAN_LAUNCHER_TOKEN: str = ""

settings = Settings()
fleet_settings = FleetSettings()

# The deterministic root UUID every pre-#1876 catalog row was backfilled with
# (Backend migration AddDataRootIdToDataLakeArtifactsAndRuns) and the value
# active_root_id() falls back to when DATA_LAKE_ROOT_ID is unset. Lives here,
# not in app.data_lake.root_identity, so that module — which needs
# app.data_lake.path_policy for its RootContext — has no reason to be
# imported by app.data_lake.types (whose Pydantic models default their own
# data_root_id fields to active_root_id()); path_policy itself imports
# types.py for PriceAdjustmentMode, so routing this through root_identity
# would be a straight import cycle. config.py has no data_lake dependents to
# cycle with, and "which UUID does this deployment call its active root" is
# config-shaped regardless.
LEGACY_ROOT_ID = UUID("00000000-0000-0000-0000-000000000000")


def active_root_id() -> UUID:
    """The service's own configured root — server-resolved, never supplied
    by a client (fixed design decision, issue #1876)."""
    return UUID(settings.DATA_LAKE_ROOT_ID) if settings.DATA_LAKE_ROOT_ID else LEGACY_ROOT_ID
