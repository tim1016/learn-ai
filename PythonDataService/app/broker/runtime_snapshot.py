"""PRD #619-A — typed boundary between live-instance callers and
``IbkrClient``.

``live_instances._resolve_safety_verdict_final`` and
``_fetch_broker_connected_account`` historically read attributes that do
not exist on the real client (``client.config.port``,
``client.account_id``, ``client.config.read_only_api``) and then swallowed
the resulting ``AttributeError`` under a bare ``except Exception``. The
silent fall-through produced an ``unknown`` verdict every time, which
masked the regression for an entire PR cycle.

This module provides one typed snapshot model + builder that only reads
public ``IbkrClient`` API (``settings.port``, ``settings.readonly``,
``settings.mode``, ``connected_account``, ``is_connected()``,
``connection_state``). The only expected exception is
``NotConnectedError`` from a missing or torn-down singleton — it returns
a snapshot with ``client_available=False`` rather than propagating, so
callers always get a structured value to consult.

ADR-0011 amendment: the snapshot carries ``readonly`` for diagnostic
display but the verdict derivation no longer treats ``readonly=False`` as
an unknown gate. ``paper-only`` is identity (mode + port + DU prefix);
order capability is a separate fact carried at the run/spec level.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.broker.ibkr.client import IbkrClient, NotConnectedError
from app.broker.ibkr.models import ClientConnectionState


class BrokerRuntimeSnapshot(BaseModel):
    """Structured read of the live ``IbkrClient`` for safety/account use.

    The builder NEVER raises for the disconnect path — that case returns
    a snapshot with ``client_available=False``. Any other failure to
    construct this model is a programming error and is allowed to
    propagate so the regression surfaces.
    """

    model_config = ConfigDict(frozen=True)

    client_available: bool
    connected: bool
    configured_mode: Literal["paper", "live"] | None
    readonly: bool | None
    port: int | None
    connected_account: str | None
    connection_state: ClientConnectionState | None


_UNAVAILABLE = BrokerRuntimeSnapshot(
    client_available=False,
    connected=False,
    configured_mode=None,
    readonly=None,
    port=None,
    connected_account=None,
    connection_state=None,
)


def build_broker_runtime_snapshot(client: IbkrClient | None) -> BrokerRuntimeSnapshot:
    """Read a snapshot from the live singleton.

    ``client is None`` and a singleton that raises ``NotConnectedError``
    both reduce to ``client_available=False``. Every other field reads
    from public API only.
    """
    if client is None:
        return _UNAVAILABLE

    settings = client.settings
    configured_mode: Literal["paper", "live"] | None = (
        settings.mode if settings.mode in ("paper", "live") else None
    )

    try:
        connected = client.is_connected()
    except NotConnectedError:
        connected = False

    return BrokerRuntimeSnapshot(
        client_available=True,
        connected=connected,
        configured_mode=configured_mode,
        readonly=settings.readonly,
        port=settings.port,
        connected_account=client.connected_account,
        connection_state=client.connection_state,
    )


def make_live_engine_verdict_provider(client: IbkrClient):
    """PRD #619-A §A3 — build a ``verdict_provider`` closure for ``LiveEngine``.

    ``cmd_start`` constructs the child's own ``IbkrClient``; this
    closure reads the live snapshot via public API and returns the
    ADR-0011 identity verdict literal that
    ``LiveEngine._check_verdict_transition_halt`` consumes. The engine
    writes the result to ``verdict_snapshot.json`` on every check — no
    new transport, no behaviour change in the engine itself.

    Authority: the closure reads the *child's own* ``IbkrClient`` — the
    process that actually places (or refuses to place) orders. The
    data-plane FastAPI singleton's observation never feeds this path.

    ADR-0011 amendment: ``readonly`` is passed through to the per-gate
    breakdown but no longer participates in identity derivation;
    capability is composed separately at the Resume gate.
    """
    from app.broker.safety_verdict import derive_broker_safety_verdict

    def _provider() -> str:
        s = build_broker_runtime_snapshot(client)
        return derive_broker_safety_verdict(
            configured_mode=s.configured_mode,
            readonly_flag=s.readonly,
            port=s.port,
            connected_account=s.connected_account,
        ).final_verdict

    return _provider


def snapshot_data_plane_broker() -> BrokerRuntimeSnapshot:
    """Snapshot the FastAPI data-plane singleton, if any.

    Returns ``client_available=False`` when the broker subsystem is
    disabled (``IBKR_BROKER_ENABLED=false``) or the lifespan event has
    not constructed a client yet. Any other failure is allowed to
    propagate — the historical broad ``except Exception`` is what hid
    the regression PRD #619-A is fixing.

    PRD #619-A note: the singleton snapshot is the *fleet/data-plane*
    observation — it is advisory only for live runs. Per-run safety
    authority lives in the live-engine child via ``verdict_snapshot.json``.
    """
    from app.broker.ibkr.client import get_client
    from app.broker.ibkr.config import get_settings

    if not get_settings().broker_enabled:
        return _UNAVAILABLE

    try:
        client = get_client()
    except NotConnectedError:
        return _UNAVAILABLE

    return build_broker_runtime_snapshot(client)
