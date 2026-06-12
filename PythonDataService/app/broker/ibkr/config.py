"""IBKR connection settings, env-var-backed.

Three layers of paper-vs-live safety are enforced at this seam:

1. ``IBKR_MODE`` — explicit ``paper`` / ``live`` selector. Default is
   ``paper``. Refuses ``live`` unless the operator explicitly sets it.
2. **Port-vs-mode validator.** Live ports (TWS 7496, Gateway 4001) are
   rejected when ``IBKR_MODE=paper``; paper ports (7497, 4002) are
   rejected when ``IBKR_MODE=live``. A simple typo cannot silently
   route a paper-mode build to a live socket.
3. The runtime ``client.IbkrClient.connect`` adds a third layer — the
   account-ID sentinel check (paper IDs begin with ``DU``).

This module deliberately does not depend on ``app.config.settings`` —
keep the broker concern isolated so a future refactor of the global
settings module does not ripple into the safety boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Port → semantic mapping per IBKR docs. Authoritative reference:
# https://interactivebrokers.github.io/tws-api/initial_setup.html
PAPER_PORTS: frozenset[int] = frozenset({7497, 4002})
LIVE_PORTS: frozenset[int] = frozenset({7496, 4001})


class IbkrSettings(BaseSettings):
    """Settings for the IBKR client.

    All fields can be overridden via environment variables (``IBKR_*``)
    or a ``.env`` file. ``IBKR_MODE`` is the master switch: nothing
    treats this build as ``live`` unless this is explicitly set.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="IBKR_",
        case_sensitive=False,
        extra="ignore",
    )

    # paper | live. Default paper — refuse to default to live ever.
    mode: Literal["paper", "live"] = "paper"

    # IB Gateway / TWS host. Three valid forms:
    #   * ``auto``                — read /proc/net/route for the
    #                               container's default gateway. This is
    #                               the most reliable option under Podman
    #                               on Windows, where ``host.docker.internal``
    #                               does not always route to the Windows host.
    #   * ``host.docker.internal`` — works under Docker Desktop on Windows
    #                               and macOS; unreliable under Podman.
    #   * a literal IP            — e.g. ``192.168.1.5`` or ``10.0.2.2``,
    #                               used as-is. Set this when you need to
    #                               pin to a specific interface.
    # The default is ``auto`` because it works in every container runtime
    # we deploy to; native (non-containerised) callers should set
    # ``IBKR_HOST=127.0.0.1``.
    host: str = "auto"

    # 4002 = Gateway paper, 4001 = Gateway live, 7497 = TWS paper, 7496 = TWS live.
    port: int = Field(default=4002, ge=1, le=65535)

    # Each client connecting to one Gateway instance must use a unique
    # ``client_id``. Reserve 1 for the FastAPI lifespan client; later
    # phases (e.g. background recorder) get higher IDs.
    client_id: int = Field(default=1, ge=0, le=2**31 - 1)

    # Connect attempts before the lifespan event surfaces a startup
    # failure. Each attempt is a 5-second timeout inside ib_async.
    connect_attempts: int = Field(default=3, ge=1, le=10)

    # Operator-controlled lockdown for order placement. Enforced in
    # ``orders._enforce_paper_safety`` (Layer 0) — when True, every call
    # to ``place_paper_order`` raises ``OrderRefusedError`` before any
    # contract is built. Also passed to ``ib_async.IB.connectAsync`` for
    # its startup-fetch optimization, but note that flag does NOT block
    # placeOrder server-side; the real gate is in our Python code.
    # Default True — operators must explicitly set ``IBKR_READONLY=false``
    # in .env to enable Phase 3 order-placement endpoints.
    readonly: bool = True

    # Tick stream → Parquet archive. Default OFF; flip to True once the
    # archive directory has been chosen and the writer is in place
    # (Phase 1.5 follow-up — see persistence.py).
    persist_ticks: bool = False

    # Account snapshot persistence (Phase 2c). Same pattern as ticks: the
    # writer is in place, default OFF, flip when forensic queries become
    # necessary. One file per UTC date, ``account.parquet``.
    persist_account: bool = False

    # P&L tick persistence (Phase 2c). Default OFF. One file per
    # (UTC date, account_id), ``pnl.parquet`` with both account-level
    # (con_id NULL) and per-position rows.
    persist_pnl: bool = False

    # Where Parquet partitions land when any persist flag is True.
    # Created lazily under ``{persist_dir}/{date}/{topic}.parquet``.
    persist_dir: str = "/data/ibkr-ticks"

    # Enable/disable the IBKR client entirely. Set False when a host-venv
    # cmd_start process owns the IBKR session (client_id=42); the container
    # stays up to serve the live-runs artifact router but does not connect.
    broker_enabled: bool = True

    # Auto-connect during FastAPI startup. When False, the lifespan
    # instantiates the IbkrClient but does NOT call connect(); the operator
    # is expected to drive the lifecycle from the Status page via
    # ``POST /api/broker/connect``. ``broker_enabled=false`` takes precedence —
    # this flag has no effect when the broker subsystem is disabled.
    connect_on_startup: bool = True

    # Artifact root for live-run directories. Host path differs from container
    # path due to the compose volume mount; surfaced as config so tests can
    # parameterise it. Container default: /app/artifacts/live_runs.
    live_runs_root: str = "/app/artifacts/live_runs"

    # Persistent JSONL + Parquet root for live bars (Slice 4). The
    # ``LiveBarAggregator`` writes every emitted bar here and replays
    # today's bars from here on subscribe so a restart hands the chart
    # today's bars before the stream produces a single new one. Shares the
    # ``artifacts/`` mount so retention and disk-usage live with the run
    # artifacts.
    live_bars_root: str = "/app/artifacts/live_bars"

    # Retention window for the live-bar persistence layer. Files older than
    # this are removed by the periodic retention sweep; quarantined files
    # are kept regardless as forensic evidence (see BarPersistence).
    live_bars_retention_days: int = 30

    # Host-side live-run daemon (ADR 0004). The instance-status endpoint queries
    # it for the authoritative live strategy_instance_id -> run_id binding —
    # "live" is a process fact only the registry can prove. The container reaches
    # the host daemon via host.containers.internal (same pattern as
    # LEAN_LAUNCHER_URL); the daemon defaults to port 8765.
    live_runner_daemon_url: str = "http://host.containers.internal:8765"

    # Fleet policy gate (ADR 0005, #399). When True, account-level contamination
    # (a position no managed instance created) blocks starts across the account.
    # Default False — contamination is surfaced as a DEGRADED banner but does not
    # silently block an executing strategy's own readiness.
    fleet_dirty_blocks_starts: bool = False

    @model_validator(mode="after")
    def _enforce_port_mode_consistency(self) -> IbkrSettings:
        """Refuse to run with a port that disagrees with ``mode``.

        This is the second of the three paper-vs-live safety layers. The
        misconfiguration this catches is the one we worry about most:
        operator types ``IBKR_MODE=paper`` but leaves ``IBKR_PORT=4001``
        from a copy-pasted snippet, and the build silently connects to
        the live Gateway. We refuse to start in that state.
        """
        if self.mode == "paper" and self.port in LIVE_PORTS:
            raise ValueError(
                f"IBKR_MODE=paper but IBKR_PORT={self.port} is a LIVE port. "
                f"Paper ports are {sorted(PAPER_PORTS)}. Refusing to start."
            )
        if self.mode == "live" and self.port in PAPER_PORTS:
            raise ValueError(
                f"IBKR_MODE=live but IBKR_PORT={self.port} is a PAPER port. "
                f"Live ports are {sorted(LIVE_PORTS)}. Refusing to start."
            )
        return self


# Module-level singleton — instantiated lazily so tests can monkeypatch
# environment before the first read.
_settings: IbkrSettings | None = None


def get_settings() -> IbkrSettings:
    """Return the process-wide IBKR settings, instantiated on first use."""
    global _settings
    if _settings is None:
        _settings = IbkrSettings()
    return _settings


def reset_settings_for_testing() -> None:
    """Reset the cached settings — for tests that mutate env mid-suite."""
    global _settings
    _settings = None
