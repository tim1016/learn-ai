"""TCP keep-alive for the ib_async transport socket.

A silently severed bridge (Podman host-alias hiccup, intermediate router
failure) leaves the IBKR socket open and idle until OS-default keep-alive
probes (~2h on Linux). By the time the kernel notices, the AutoReconnectMonitor
has been polling a stale ``isConnected() == True`` for hours and the
operator's live session is dead without any UI signal. Setting kernel
keep-alive shortens detection to ~60s so the monitor's next poll sees the
flipped socket state and reconnects.

The path through ib_async 2.x to the underlying asyncio Transport is pinned:
``ib.client.conn.transport`` is set by ``loop.create_connection(...)`` inside
``Connection.connectAsync`` (see ``ib_async.connection.Connection``). The
Transport's ``get_extra_info("socket")`` is standard asyncio API. We do NOT
guard the chain with ``getattr`` — if the contract changes in a future
ib_async major bump, the test exercising this module will fail loud, which
is exactly what we want.
"""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_async import IB

logger = logging.getLogger(__name__)


# Worst-case detection of a dead bridge: IDLE + INTVL * CNT ≈ 60s. Tighter
# than IBKR's own 1100 cadence, so a silently severed bridge surfaces while
# the operator can still recover the session.
IDLE_S = 30
INTVL_S = 10
CNT = 3


def apply_tcp_keepalive(ib: IB) -> None:
    """Enable TCP keep-alive on the connected ib_async transport socket.

    Caller invariant: ``ib`` is a connected ``ib_async.IB`` — i.e.
    ``ib.client.conn.transport`` is populated. Calling against an
    unconnected ``IB`` is a programmer error, not a runtime fallback.

    Linux-only ``TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT`` are gated on
    ``hasattr`` so a macOS or BSD build still gets ``SO_KEEPALIVE``
    without crashing. A failed ``setsockopt`` (rare; the socket is
    process-owned by us) is logged at WARNING and swallowed — the
    AutoReconnectMonitor catches any drops keep-alive would have
    accelerated.
    """
    sock = ib.client.conn.transport.get_extra_info("socket")
    if sock is None:
        # The asyncio Transport contract permits returning None when the
        # backing socket isn't reachable (loop-of-loops shims, in-process
        # test transports). Real kernel sockets always return one.
        logger.warning(
            "ib_async transport reported no socket; skipping TCP keep-alive",
            extra={"action": "tcp_keepalive_skip"},
        )
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, IDLE_S)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, INTVL_S)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, CNT)
    except OSError as exc:
        logger.warning(
            "setsockopt failed on IBKR socket: %s",
            exc,
            extra={"action": "tcp_keepalive_skip"},
        )
        return
    logger.info(
        "TCP keep-alive enabled on IBKR socket (idle=%ds intvl=%ds cnt=%d)",
        IDLE_S,
        INTVL_S,
        CNT,
        extra={"action": "tcp_keepalive_set"},
    )
