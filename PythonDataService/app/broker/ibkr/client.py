"""IBKR connection lifecycle.

Wraps ``ib_async.IB`` for the rest of the broker subpackage. Public
surface:

* ``IbkrClient`` — async context-manageable singleton-ish wrapper. The
  FastAPI lifespan event owns one instance; downstream code reads it via
  ``get_client()``.
* ``ConnectionRefusedDueToSentinelError`` — raised when the connected
  account ID does not match the configured ``IBKR_MODE``. This is the
  third paper-vs-live safety layer (env-var → port-validator →
  account-ID sentinel).

Per the repo's "tight coupling internally, curated externally" rule,
this module exposes the underlying ``IB()`` instance for in-package use
(``app.broker.ibkr.market_data`` etc.) but the FastAPI router never
touches it directly.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.broker.ibkr.config import IbkrSettings, get_settings
from app.broker.ibkr.models import IbkrConnectionHealth

logger = logging.getLogger(__name__)


# Structured connection state surfaced to the cockpit. ``connected`` and
# ``disconnected`` are observable from the socket; ``soft_lost`` means the
# socket is open but IBKR signalled connectivity loss (Error 1100);
# ``reconnecting`` is set by the AutoReconnectMonitor while it is mid-
# attempt; ``disabled`` is the broker-disabled-via-env case.
ConnectionState = Literal[
    "connected", "soft_lost", "reconnecting", "disconnected", "disabled"
]


# TCP keep-alive parameters. Worst-case detection of a dead bridge:
# IDLE + INTVL * CNT ≈ 60s. Tighter than IBKR's own 1100 cadence, so a
# silently severed bridge surfaces while the operator can still recover
# the session rather than discovering it hours later when the OS-default
# (2h) keep-alive finally probes. Linux-only socket options
# (TCP_KEEPIDLE/INTVL/CNT) are gated on ``hasattr`` so a macOS / BSD
# build still gets SO_KEEPALIVE without crashing.
_TCP_KEEPIDLE_S = 30
_TCP_KEEPINTVL_S = 10
_TCP_KEEPCNT = 3


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


# TWS/IB connectivity error codes that the ``errorEvent`` handler reacts to.
# 1100 = "Connectivity between IB and TWS has been lost"; 504 = "Not
# connected". Both mean the data feed is dead even though the API socket to
# TWS may still report ``isConnected() == True``. 1101 = connectivity restored
# (data maintained); 1102 = connectivity restored (data lost). See
# https://interactivebrokers.github.io/tws-api/message_codes.html.
_CONNECTIVITY_LOST_CODES = frozenset({1100, 504})
_CONNECTIVITY_RESTORED_CODES = frozenset({1101, 1102})


# Sentinel value for ``IBKR_HOST`` that triggers host auto-resolution.
#
# Resolution order (first usable wins):
#   1. The Podman host alias ``host.containers.internal`` if it resolves.
#      Compose registers it via ``extra_hosts: host-gateway`` and it points
#      to the actual host machine in EVERY runtime that supports it:
#      macOS Podman (gvproxy bridges to host loopback), Linux Podman
#      rootless (bridge gateway), Docker Desktop, native Docker. Using the
#      alias rather than the bridge gateway IP makes the macOS path work
#      out of the box, where the default gateway is the Podman VM's
#      loopback — NOT the macOS host — and a bare ``IBKR_HOST=auto`` would
#      otherwise resolve to the wrong machine.
#   2. ``host.docker.internal`` if the canonical Podman name isn't
#      registered (older compose setups, plain Docker without an explicit
#      ``extra_hosts``).
#   3. ``/proc/net/route`` default-gateway parsing. This is the original
#      auto-resolution, kept as a fallback for bare-metal Podman where
#      the host aliases aren't registered.
#   4. Literal ``auto`` — surfaces as a clear DNS-style failure at the
#      wire rather than this helper silently returning a wrong host.
HOST_AUTO_SENTINEL = "auto"

# In preference order. Both names point to the host machine on every Podman /
# Docker runtime that registers them; we try ``host.containers.internal`` first
# because it is the Podman-native spelling (the project's compose.yaml ships
# it on ``python-service``).
_PREFERRED_HOST_ALIASES: tuple[str, ...] = (
    "host.containers.internal",
    "host.docker.internal",
)


def _resolve_host_alias(aliases: tuple[str, ...] = _PREFERRED_HOST_ALIASES) -> str | None:
    """Return the first ``aliases`` entry that resolves to an IP, else None.

    Resolution goes through ``socket.gethostbyname`` — these aliases are
    registered in ``/etc/hosts`` by Podman/Docker's ``extra_hosts`` config,
    so the lookup is a local file read, not a DNS round-trip. We return the
    alias *name* rather than the resolved IP so logs/diagnostics read as
    e.g. ``host.containers.internal`` (operator-recognizable) instead of
    ``192.168.127.254`` (gvproxy's bridge address, which means nothing to a
    human). ``ib_async.connectAsync`` re-resolves at connect time anyway.
    """
    import socket  # local import: stdlib, no runtime cost to defer

    for alias in aliases:
        try:
            socket.gethostbyname(alias)
        except OSError:
            continue
        return alias
    return None


def _detect_host_gateway(route_file: str | Path = "/proc/net/route") -> str | None:
    """Return the container's default gateway IP, or ``None`` if not found.

    Parses ``/proc/net/route`` directly (no ``ip`` binary required). The
    default route is identified by ``Destination == 00000000`` and the
    ``RTF_GATEWAY`` flag (``0x2``) set. The gateway field is little-endian
    hex; we reverse the byte order to produce a dotted-quad string.

    Inside a container this is the IP of the host machine (or the bridge
    that NATs to the host), which is the right target for IB Gateway
    running natively on Windows.
    """
    try:
        with open(route_file) as fh:
            lines = fh.readlines()
    except OSError as exc:
        logger.warning("Could not read %s for host detection: %s", route_file, exc)
        return None

    # First line is the header; skip it.
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        destination, gateway_hex, flags = fields[1], fields[2], fields[3]
        if destination != "00000000":
            continue
        try:
            if not (int(flags, 16) & 0x2):  # RTF_GATEWAY
                continue
            # Little-endian hex → dotted quad.
            octets = [str(int(gateway_hex[i : i + 2], 16)) for i in (6, 4, 2, 0)]
            return ".".join(octets)
        except (ValueError, IndexError):
            continue
    return None


def _resolve_host(configured: str) -> str:
    """If the configured host is the AUTO sentinel, resolve to the host machine.

    See the ``HOST_AUTO_SENTINEL`` docstring for the full resolution order.
    The alias path is preferred because on macOS Podman applehv the bridge
    gateway resolves to the *Podman VM's* loopback rather than the actual
    macOS host where IB Gateway runs — every fresh macOS bootstrap hit
    ``ConnectionRefusedError(111)`` until the operator manually set
    ``IBKR_HOST=host.containers.internal``.
    """
    if configured != HOST_AUTO_SENTINEL:
        return configured
    alias = _resolve_host_alias()
    if alias is not None:
        logger.info("IBKR_HOST=auto resolved via container host alias %s", alias)
        return alias
    detected = _detect_host_gateway()
    if detected is not None:
        logger.info(
            "IBKR_HOST=auto: no container host alias registered, fell back to "
            "default gateway %s",
            detected,
        )
        return detected
    logger.error(
        "IBKR_HOST=auto but neither a container host alias (host.containers.internal "
        "/ host.docker.internal) nor a default gateway in /proc/net/route could be "
        "resolved. Falling back to the literal 'auto', which will fail at the wire — "
        "set IBKR_HOST explicitly in your .env."
    )
    return configured


class BrokerError(Exception):
    """Base for broker integration errors."""


class ConnectionRefusedDueToSentinelError(BrokerError):
    """Raised when the live account ID disagrees with ``IBKR_MODE``.

    Paper IBKR account IDs start with ``DU``. If we're configured for
    paper but the connected account is a live one, abort the connection
    rather than proceed.
    """


class NotConnectedError(BrokerError):
    """Raised when an operation that requires a live connection is invoked
    while the client is disconnected."""


class IbkrClientIdInUseError(BrokerError):
    """Raised when IB Gateway / TWS rejects the connect with error 326.

    TWS error 326 ("Unable to connect as the client id is already in use")
    means the requested ``clientId`` is held by another open API session
    on the same Gateway — or, more commonly, by a stale half-open
    connection from a prior crashed session that hasn't timed out yet.

    Retrying the same ``clientId`` does NOT resolve this: the slot stays
    reserved until either the zombie socket times out (which can take
    minutes) or IB Gateway is restarted. So this error is raised
    immediately on the first attempt rather than being absorbed into a
    generic ``BrokerError`` after the full retry budget expires —
    surfacing the actual remediation (restart Gateway or bump
    ``IBKR_CLIENT_ID``) instead of hiding it under ``TimeoutError``.
    """


def _is_paper_account(account_id: str) -> bool:
    """Paper accounts at IBKR begin with ``DU``."""
    return account_id.upper().startswith("DU")


class IbkrClient:
    """Lifecycle wrapper around ``ib_async.IB``.

    Not thread-safe — designed for a single-process FastAPI app where the
    lifespan event is the sole owner. ``ib_async`` itself is asyncio-
    based; cross-thread access is not supported by the library.
    """

    def __init__(self, settings: IbkrSettings | None = None) -> None:
        # Defer the ib_async import so importing this module does not
        # require ib_async to be installed in environments that never
        # touch the broker (CI for unrelated tests, local-dev shells
        # without the heavy layer, etc.).
        from ib_async import IB

        self._settings = settings or get_settings()
        self._ib: IB = IB()
        self._connected_account: str | None = None
        # Tracks whether TWS error 326 ("client id already in use") has
        # surfaced during the current connect attempt. ib_async logs this
        # via the wrapper but raises only TimeoutError from connectAsync,
        # hiding the actionable cause. Hook errorEvent so connect() can
        # fast-fail with the real remediation message.
        self._client_id_in_use_seen: bool = False
        # Soft connectivity loss (TWS 1100) leaves the API socket open, so
        # ``isConnected()`` keeps returning True while the data feed is dead.
        # Track that condition explicitly so streaming loops can halt instead
        # of hanging on a silently-frozen feed. Cleared on connect and on a
        # restore event (1101/1102). The counter is observable (logged + read
        # by diagnostics) per numerical-rigor's "surfaced, never silenced".
        self._connection_lost: bool = False
        self._connectivity_lost_count: int = 0
        # Auto-reconnect bookkeeping the AutoReconnectMonitor publishes into.
        # The cockpit reads ``connection_state`` / ``reconnect_attempt`` /
        # ``last_transition_ms`` to render the live link state without ever
        # claiming "Connected" while the monitor is mid-attempt.
        self._reconnecting: bool = False
        self._reconnect_attempt: int = 0
        self._successful_reconnect_count: int = 0
        self._last_transition_ms: int = _now_ms()
        self._last_connection_state: ConnectionState = "disconnected"
        self._ib.errorEvent += self._on_ib_error

    def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """ib_async errorEvent handler.

        Reacts to three classes of code and ignores the rest:

        * ``326`` — clientId already in use (captured for ``connect()``'s
          fast-fail).
        * ``1100`` / ``504`` — connectivity to TWS/IB lost. Mark the
          connection degraded so streaming loops surface a fatal error rather
          than hanging on a feed that has gone dark. ``isConnected()`` can
          still report True here because the API socket stays open.
        * ``1101`` / ``1102`` — connectivity restored. Clear the flag.
        """
        if errorCode == 326:
            self._client_id_in_use_seen = True
            return
        if errorCode in _CONNECTIVITY_LOST_CODES:
            self._connection_lost = True
            self._connectivity_lost_count += 1
            logger.warning(
                "IBKR connectivity lost",
                extra={
                    "error_code": errorCode,
                    "error": errorString,
                    "action": "connection_lost",
                },
            )
            return
        if errorCode in _CONNECTIVITY_RESTORED_CODES:
            self._connection_lost = False
            logger.info(
                "IBKR connectivity restored",
                extra={
                    "error_code": errorCode,
                    "error": errorString,
                    "action": "connection_restored",
                },
            )

    # ── lifecycle ───────────────────────────────────────────────────────

    async def connect(self) -> IbkrConnectionHealth:
        """Connect to IB Gateway / TWS and assert the sentinel.

        Retries up to ``settings.connect_attempts`` with a short backoff.
        On success, asserts that the connected account agrees with
        ``mode``. On disagreement, immediately disconnects and raises
        ``ConnectionRefusedDueToSentinelError`` — we do not leave a
        wrong-mode connection open.
        """
        s = self._settings
        resolved_host = _resolve_host(s.host)
        last_error: Exception | None = None

        for attempt in range(1, s.connect_attempts + 1):
            self._client_id_in_use_seen = False
            self._connection_lost = False
            try:
                logger.info(
                    "[STEP 1/3] IBKR connect attempt %d/%d → %s:%d (mode=%s, clientId=%d)",
                    attempt,
                    s.connect_attempts,
                    resolved_host,
                    s.port,
                    s.mode,
                    s.client_id,
                )
                await self._ib.connectAsync(
                    host=resolved_host,
                    port=s.port,
                    clientId=s.client_id,
                    readonly=s.readonly,
                )
                # Enable TCP keep-alive immediately so a dead bridge
                # surfaces in ~60s rather than the OS-default 2h. Failure
                # to set keep-alive does not abort the connect — the
                # AutoReconnectMonitor catches missed drops as a fallback.
                self._apply_tcp_keepalive()
                break
            except Exception as exc:
                last_error = exc
                if self._client_id_in_use_seen:
                    raise IbkrClientIdInUseError(
                        f"IBKR clientId {s.client_id} is already in use on "
                        f"Gateway at {resolved_host}:{s.port}. The slot will "
                        f"not free up on retry — remediation: restart IB "
                        f"Gateway to clear the zombie session, or set a "
                        f"different IBKR_CLIENT_ID in .env."
                    ) from exc
                logger.warning(
                    "IBKR connect attempt %d failed: %s",
                    attempt,
                    exc,
                )
                await asyncio.sleep(min(2.0 * attempt, 5.0))
        else:
            raise BrokerError(
                f"IBKR connect failed after {s.connect_attempts} attempts; last error: {last_error!r}"
            ) from last_error

        # ── sentinel check ──────────────────────────────────────────────
        accounts = list(self._ib.managedAccounts())
        if not accounts:
            self._ib.disconnect()
            raise BrokerError("Connected to IBKR but managedAccounts() returned empty.")

        # Single-account assumption holds for individual paper/live setups.
        # Multi-account FA structures need explicit selection — fail closed
        # rather than silently use ``accounts[0]``, because the sentinel
        # check below would only validate one of the accessible accounts
        # while orders could still route to a sibling account that
        # disagrees with ``IBKR_MODE``.
        if len(accounts) > 1:
            self._ib.disconnect()
            raise BrokerError(
                f"IBKR returned {len(accounts)} managed accounts ({accounts!r}). "
                "Multi-account selection is not yet implemented. Refusing to "
                "proceed because the paper/live sentinel can only validate one "
                "account at a time."
            )
        account_id = accounts[0]
        is_paper = _is_paper_account(account_id)

        if s.mode == "paper" and not is_paper:
            self._ib.disconnect()
            raise ConnectionRefusedDueToSentinelError(
                f"IBKR_MODE=paper but connected account {account_id!r} is NOT a paper "
                f"account (paper IDs begin with 'DU'). Disconnected. Refusing to proceed."
            )
        if s.mode == "live" and is_paper:
            self._ib.disconnect()
            raise ConnectionRefusedDueToSentinelError(
                f"IBKR_MODE=live but connected account {account_id!r} IS a paper "
                f"account. Disconnected. Refusing to proceed."
            )

        self._connected_account = account_id
        logger.info(
            "[STEP 3/3] IBKR connected: account=%s is_paper=%s server_version=%s",
            account_id,
            is_paper,
            self._ib.client.serverVersion() if self._ib.client else None,
        )
        return self.health()

    async def disconnect(self) -> None:
        """Idempotent disconnect."""
        if self._ib.isConnected():
            # ib_async.IB only exposes a synchronous disconnect(); there is
            # no disconnectAsync. The smoke run on 2026-05-13 surfaced this
            # the first time cmd_start ever called us in production.
            self._ib.disconnect()
        self._connected_account = None

    # ── accessors ───────────────────────────────────────────────────────

    @property
    def settings(self) -> IbkrSettings:
        return self._settings

    @property
    def ib(self):
        """Underlying ``ib_async.IB`` for in-package use only.

        Routers MUST NOT import this. Use the curated wrappers in
        ``market_data``, ``contracts``, etc.
        """
        return self._ib

    @property
    def connected_account(self) -> str | None:
        return self._connected_account

    def is_connected(self) -> bool:
        return bool(self._ib.isConnected())

    @property
    def connection_lost(self) -> bool:
        """True between a TWS connectivity-lost (1100/504) and its restore.

        ``isConnected()`` can still be True in this window because the API
        socket to TWS stays open while TWS's own uplink to IB is down.
        """
        return self._connection_lost

    @property
    def connectivity_lost_count(self) -> int:
        """Observable count of connectivity-lost events seen this process."""
        return self._connectivity_lost_count

    def require_connected(self) -> None:
        if not self.is_connected():
            raise NotConnectedError("IBKR client is not connected.")

    def require_live(self) -> None:
        """Stricter than ``require_connected``: also fails on a soft loss.

        Streaming loops call this every iteration so a mid-session disconnect
        — hard (socket closed) or soft (TWS 1100, socket open but feed dead) —
        surfaces as a fatal error instead of an indefinite silent hang.
        """
        if not self.is_connected():
            raise NotConnectedError("IBKR client is not connected.")
        if self._connection_lost:
            raise NotConnectedError(
                "IBKR connectivity lost (TWS error 1100): the API socket is "
                "open but the data feed is down. Halting rather than streaming "
                "stale values."
            )

    def health(self) -> IbkrConnectionHealth:
        connected = self.is_connected()
        sv: int | None = None
        if connected and self._ib.client is not None:
            try:
                sv = int(self._ib.client.serverVersion())
            except Exception:
                sv = None
        state = self.connection_state
        # Stamp transition timestamps on observed changes so the cockpit
        # can render "Reconnecting (15s ago)" without an extra signal.
        if state != self._last_connection_state:
            self._last_transition_ms = _now_ms()
            self._last_connection_state = state
        return IbkrConnectionHealth(
            mode=self._settings.mode,
            host=self._settings.host,
            port=self._settings.port,
            client_id=self._settings.client_id,
            connected=connected,
            account_id=self._connected_account,
            is_paper=(_is_paper_account(self._connected_account) if self._connected_account else None),
            server_version=sv,
            fetched_at_ms=_now_ms(),
            connection_state=state,
            connection_lost=self._connection_lost,
            connectivity_lost_count=self._connectivity_lost_count,
            reconnect_attempt=self._reconnect_attempt if self._reconnecting else None,
            successful_reconnect_count=self._successful_reconnect_count,
            last_transition_ms=self._last_transition_ms,
        )

    # ── connection-state machine ────────────────────────────────────────

    @property
    def connection_state(self) -> ConnectionState:
        """Cockpit-facing state. Derivation order matters: ``reconnecting``
        wins while the monitor is mid-attempt (even if the underlying
        socket is briefly up between retries), then the socket-closed
        case, then the soft-loss case, then connected."""
        if self._reconnecting:
            return "reconnecting"
        if not self.is_connected():
            return "disconnected"
        if self._connection_lost:
            return "soft_lost"
        return "connected"

    @property
    def reconnect_attempt(self) -> int:
        """Current monitor attempt number; 0 when not reconnecting."""
        return self._reconnect_attempt if self._reconnecting else 0

    @property
    def successful_reconnect_count(self) -> int:
        """Observable count of monitor-driven recoveries this process."""
        return self._successful_reconnect_count

    def _mark_reconnect_started(self, attempt: int) -> None:
        """Called by the AutoReconnectMonitor when a fresh attempt begins.

        Bumps ``last_transition_ms`` so the cockpit's "since" age resets
        per attempt instead of dragging from the original disconnect.
        """
        self._reconnecting = True
        self._reconnect_attempt = attempt
        self._last_transition_ms = _now_ms()

    def _mark_reconnect_resolved(self, *, success: bool) -> None:
        """Called by the AutoReconnectMonitor when an attempt finishes.

        Success transitions to ``connected``; failure leaves the
        underlying ``_connection_lost`` / socket state untouched (the
        monitor will sleep and retry). In both cases the bookkeeping
        clears so the next tick's state derivation is clean.
        """
        self._reconnecting = False
        if success:
            self._reconnect_attempt = 0
            self._successful_reconnect_count += 1
        self._last_transition_ms = _now_ms()

    # ── socket-level keep-alive ─────────────────────────────────────────

    def _apply_tcp_keepalive(self) -> None:
        """Best-effort TCP keep-alive on the ib_async transport socket.

        Defensive: if the socket isn't reachable (older ib_async, a
        different transport, a test harness) we log and proceed. The
        AutoReconnectMonitor catches any drops keep-alive would have
        accelerated, so this stays a hardening tweak, not a correctness
        requirement. Without this, a silent bridge failure can sit on a
        stale-but-open socket until OS-default keep-alive (~2h) probes.
        """
        try:
            sock = self._extract_transport_socket()
        except Exception as exc:
            logger.warning(
                "Could not enable TCP keep-alive on IBKR socket: %s",
                exc,
                extra={"action": "tcp_keepalive_skip"},
            )
            return
        if sock is None:
            logger.warning(
                "IBKR transport socket not accessible; skipping TCP keep-alive",
                extra={"action": "tcp_keepalive_skip"},
            )
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _TCP_KEEPIDLE_S)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _TCP_KEEPINTVL_S)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _TCP_KEEPCNT)
        except OSError as exc:
            logger.warning(
                "Failed to set TCP keep-alive options on IBKR socket: %s",
                exc,
                extra={"action": "tcp_keepalive_skip"},
            )
            return
        logger.info(
            "TCP keep-alive enabled on IBKR socket (idle=%ds intvl=%ds cnt=%d)",
            _TCP_KEEPIDLE_S,
            _TCP_KEEPINTVL_S,
            _TCP_KEEPCNT,
            extra={"action": "tcp_keepalive_set"},
        )

    def _extract_transport_socket(self) -> socket.socket | None:
        """Reach through ``ib_async.IB`` to its asyncio transport socket.

        ib_async's connection layer evolved across versions; this tries
        the common attribute paths and returns ``None`` rather than
        raising if none of them work."""
        client = self._ib.client
        if client is None:
            return None
        # ib_async ≥ 0.9: client.conn.writer (asyncio.StreamWriter)
        conn = getattr(client, "conn", None)
        if conn is not None:
            writer = getattr(conn, "writer", None) or getattr(conn, "_writer", None)
            if writer is not None and hasattr(writer, "get_extra_info"):
                return writer.get_extra_info("socket")
        # Older / alternate transports: client.socket directly.
        direct = getattr(client, "socket", None)
        if isinstance(direct, socket.socket):
            return direct
        return None


# ── module-level singleton ──────────────────────────────────────────────
# Instantiated by the FastAPI lifespan event in ``app.main``. Tests can
# replace via ``set_client`` without going through the lifespan.

_client: IbkrClient | None = None

# Lifecycle lock serialising ``connect`` / ``disconnect`` / ``reconnect`` across
# every entry point that drives the singleton: the broker router's operator
# endpoints AND the auto-reconnect monitor. Without a shared lock the monitor's
# tick can race an operator click and call ``connectAsync`` twice on the same
# ``ib_async.IB``, corrupting its session bookkeeping. The lock is module-level
# (not on the instance) so the "create-the-singleton-if-missing" path in the
# router can still acquire it before a client exists.
_client_lifecycle_lock = asyncio.Lock()


def get_client() -> IbkrClient:
    if _client is None:
        raise NotConnectedError(
            "IbkrClient is not initialised. The FastAPI lifespan event should construct one at startup."
        )
    return _client


def set_client(client: IbkrClient | None) -> None:
    """Install the process-wide client. Called from the lifespan event."""
    global _client
    _client = client


def get_client_lifecycle_lock() -> asyncio.Lock:
    """Return the shared connect/disconnect lock the router and monitor share."""
    return _client_lifecycle_lock
