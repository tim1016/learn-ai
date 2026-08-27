"""PRD #619-A — typed boundary between live-instance callers and
``IbkrClient``.

The former ``routers/live_instances.py``'s ``_resolve_safety_verdict_final``
and ``_fetch_broker_connected_account`` (both retired along with that
router, PR-B of #1813, 2026-08-27) historically read attributes that did
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


def snapshot_data_plane_broker() -> BrokerRuntimeSnapshot:
    """Snapshot the FastAPI data-plane singleton, if any.

    Returns ``client_available=False`` when the broker subsystem is
    disabled (``IBKR_BROKER_ENABLED=false``) or the lifespan event has
    not constructed a client yet. Any other failure is allowed to
    propagate — the historical broad ``except Exception`` is what hid
    the regression PRD #619-A is fixing.

    PRD #619-A note: the singleton snapshot is the data plane's read-only
    observation. The retired IBKR engine child no longer consumes it as an
    execution verdict.
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
