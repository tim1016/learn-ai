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
from collections import deque
from pathlib import Path

from app.broker.ibkr.config import IbkrSettings, get_settings
from app.broker.ibkr.event_codes import (
    CONNECTIVITY_LOST_CODES as _CONNECTIVITY_LOST_CODES,
)
from app.broker.ibkr.event_codes import (
    CONNECTIVITY_RESTORED_CODES as _CONNECTIVITY_RESTORED_CODES,
)
from app.broker.ibkr.event_codes import (
    DATA_FARM_DEGRADED_CODES as _DATA_FARM_DEGRADED_CODES,
)
from app.broker.ibkr.event_codes import (
    DATA_FARM_OK_CODES as _DATA_FARM_OK_CODES,
)
from app.broker.ibkr.event_codes import (
    ORDER_REJECTION_CODES as _ORDER_REJECTION_CODES,
)
from app.broker.ibkr.event_codes import (
    SUBSCRIPTIONS_STALE_CODES as _SUBSCRIPTIONS_STALE_CODES,
)
from app.broker.ibkr.keepalive import apply_tcp_keepalive
from app.broker.ibkr.models import ClientConnectionState, IbkrConnectionHealth
from app.broker.ibkr.order_error_stream import OrderErrorEvent
from app.broker.ibkr.recovery_state_machine import recovery_state_from_connection_state
from app.services.broker_session_events import BrokerSessionEventService
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_ORDER_ERROR_BUFFER_LIMIT = 512


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


def _is_order_rejection_error(req_id: int, error_code: int) -> bool:
    """True for IBKR order-rejection callbacks that can join by reqId."""

    return req_id >= 0 and error_code in _ORDER_REJECTION_CODES


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
        self._subscriptions_stale: bool = False
        self._data_farm_degraded: bool = False
        self._last_ibkr_code: int | None = None
        self._last_ibkr_message: str | None = None
        self._last_probe_ms: int | None = None
        self._last_probe_error: str | None = None
        self._last_recovery_ms: int | None = None
        self._recovery_error: str | None = None
        # Broker-event-log filesystem-write failure counter + last
        # timestamp. The emit site logs the FIRST failure per run at
        # WARNING and suppresses subsequent ones (codex D5) so a recurring
        # read-only mount can't spam the Incidents panel. Both fields are
        # surfaced via ``health()`` so the cockpit runtime banner can
        # render "evidence integrity degraded" even when trading
        # continues.
        self._broker_event_log_write_failed_count: int = 0
        self._last_broker_event_log_write_failed_at_ms: int | None = None
        # Wall-clock when the client's own observable connection state last
        # changed — set by ``connect`` / ``disconnect`` / 1100 / 1101.
        # ``health()`` returns this verbatim; the cockpit overlay composer
        # (``build_broker_health``) maxes it against the monitor's transition
        # timestamp so the wire-level ``last_transition_ms`` is the most
        # recent of either side.
        self._last_event_ms: int = now_ms_utc()
        self._order_error_events: deque[OrderErrorEvent] = deque()
        self._next_order_error_seq: int = 1
        # Operator-intended connection state. The AutoReconnectMonitor only
        # acts on observed drops when this is True; ``POST /disconnect`` sets
        # it False so the operator's "off" sticks, and a startup with
        # ``IBKR_CONNECT_ON_STARTUP=false`` leaves it False until the
        # operator clicks Connect. Codex P1 fix on PR #563.
        self._desired_connected: bool = False
        self._ib.errorEvent += self._on_ib_error

    def _record_broker_event(self, event_type: str, **fields: object) -> None:
        """Append a broker lifecycle event to the diagnostics JSONL log."""
        path = (
            Path(self._settings.live_runs_root)
            / "_broker"
            / "connection_events.jsonl"
        )
        payload = {
            "event_type": event_type,
            "ts_ms_utc": now_ms_utc(),
            "mode": self._settings.mode,
            "host": self._settings.host,
            "port": self._settings.port,
            "client_id": self._settings.client_id,
            "connected_account": self._connected_account,
            **fields,
        }
        try:
            BrokerSessionEventService(
                path=path,
                max_events=self._settings.broker_session_event_retention_count,
            ).append_event(payload)
        except OSError as exc:
            # Rate-limit per codex D5: first failure per run logs WARNING
            # (and the classifier catches it as
            # BROKER_EVENT_LOG_WRITE_FAILED → INFRA); subsequent failures
            # increment the counter + stamp the timestamp without
            # re-emitting the line. The counter + last-failure stamp
            # surface through ``health()`` so the cockpit runtime banner
            # can still render "evidence integrity degraded".
            self._broker_event_log_write_failed_count += 1
            self._last_broker_event_log_write_failed_at_ms = now_ms_utc()
            if self._broker_event_log_write_failed_count == 1:
                logger.warning(
                    "Could not append IBKR broker event log: %s",
                    exc,
                    extra={"action": "broker_event_log_write_failed"},
                )

    def record_recovery_event(self, event_type: str, **fields: object) -> None:
        """Append a monitor-owned recovery event to the diagnostics JSONL log."""
        self._record_broker_event(
            event_type,
            connection_state=self.connection_state,
            **fields,
        )

    def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """ib_async errorEvent handler.

        Reacts to three classes of code and ignores the rest:

        * ``326`` — clientId already in use (captured for ``connect()``'s
          fast-fail).
        * ``1100`` / ``1300`` / ``2110`` / ``504`` — connectivity to TWS/IB lost. Mark the
          connection degraded so streaming loops surface a fatal error rather
          than hanging on a feed that has gone dark. ``isConnected()`` can
          still report True here because the API socket stays open.
        * ``1101`` / ``1102`` — connectivity restored. Clear the flag.
          ``1101`` additionally means market-data subscriptions were lost and
          must be recreated.
        * ``2103`` / ``2105`` and ``2104`` / ``2106`` — market/historical
          data-farm degraded/restored signals. These do not necessarily mean
          account/order connectivity is gone, so they publish a degraded state
          instead of forcing a disconnect.
        """
        self._last_ibkr_code = errorCode
        self._last_ibkr_message = errorString
        self._record_broker_event(
            "IBKR_CODE",
            ibkr_code=errorCode,
            ibkr_req_id=reqId,
            message=errorString,
            connection_state=self.connection_state,
        )
        if _is_order_rejection_error(reqId, errorCode):
            self._buffer_order_error(reqId, errorCode, errorString)
        if errorCode == 326:
            self._client_id_in_use_seen = True
            return
        if errorCode in _DATA_FARM_DEGRADED_CODES:
            if not self._data_farm_degraded:
                self._last_event_ms = now_ms_utc()
            self._data_farm_degraded = True
            # The TWS code is folded into the message string (not only the
            # structured ``extra``) because the incident-taxonomy
            # classifier in ``app.services.live_log_failures`` reads the
            # formatted ``%(message)s`` from ``live.log`` and ``extra``
            # is dropped. Without the code in the message body, the
            # DATA_FARM_DEGRADED row's ``dynamic_facts["tws_code"]``
            # would always be empty in production.
            logger.warning(
                "IBKR data farm degraded (code %d)",
                errorCode,
                extra={
                    "error_code": errorCode,
                    "error": errorString,
                    "action": "data_farm_degraded",
                },
            )
            return
        if errorCode in _DATA_FARM_OK_CODES:
            if self._data_farm_degraded:
                self._last_event_ms = now_ms_utc()
            self._data_farm_degraded = False
            logger.info(
                "IBKR data farm restored",
                extra={
                    "error_code": errorCode,
                    "error": errorString,
                    "action": "data_farm_restored",
                },
            )
            return
        if errorCode in _CONNECTIVITY_LOST_CODES:
            was_lost = self._connection_lost
            self._connection_lost = True
            self._connectivity_lost_count += 1
            if not was_lost:
                # State transition (healthy → soft_lost) — stamp at the
                # mutation site so ``health()`` stays a pure read.
                self._last_event_ms = now_ms_utc()
            # Logged at INFO, not WARNING — IBKR codes 1100/504 are
            # frequent transient blips during a healthy session and the
            # auto-reconnect-monitor already surfaces the cases that
            # don't recover within one tick as a WARNING-level
            # BROKER_RECONNECT_FAILED. The classifier's app.broker.ibkr.client
            # exact-anchor still catches this row as BROKER_DISCONNECT
            # when it does fire (manual ops, edge timing) — the demotion
            # only changes whether the parse_incidents WARNING gate
            # picks it up.
            logger.info(
                "IBKR connectivity lost",
                extra={
                    "error_code": errorCode,
                    "error": errorString,
                    "action": "connection_lost",
                },
            )
            return
        if errorCode in _CONNECTIVITY_RESTORED_CODES:
            if self._connection_lost:
                # Transition (soft_lost → healthy) — same rationale as above.
                self._last_event_ms = now_ms_utc()
            self._connection_lost = False
            if errorCode in _SUBSCRIPTIONS_STALE_CODES:
                self._subscriptions_stale = True
            logger.info(
                "IBKR connectivity restored",
                extra={
                    "error_code": errorCode,
                    "error": errorString,
                    "action": "connection_restored",
                },
            )

    def _buffer_order_error(self, req_id: int, error_code: int, error_message: str) -> None:
        if len(self._order_error_events) >= _ORDER_ERROR_BUFFER_LIMIT:
            dropped = self._order_error_events.popleft()
            logger.warning(
                "IBKR order-error replay buffer full; dropping oldest callback",
                extra={
                    "dropped_order_error_seq": dropped.seq,
                    "dropped_req_id": dropped.req_id,
                },
            )
        self._order_error_events.append(
            OrderErrorEvent(
                seq=self._next_order_error_seq,
                req_id=req_id,
                error_code=error_code,
                error_message=error_message,
                ts_ms=now_ms_utc(),
            )
        )
        self._next_order_error_seq += 1

    def order_errors_after(self, seq: int) -> list[OrderErrorEvent]:
        """Return buffered order-scoped IBKR errors after ``seq`` without clearing."""

        if seq < 0:
            raise ValueError(f"order error cursor must be >= 0; got {seq}")
        return [event for event in self._order_error_events if event.seq > seq]

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
                # Enable TCP keep-alive on the now-open transport so a
                # silently severed bridge surfaces in ~60s rather than the
                # OS-default ~2h. Best-effort: the monitor catches what
                # keep-alive would have accelerated if this fails.
                apply_tcp_keepalive(self._ib)
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
        self.mark_recovery_succeeded()
        self._subscriptions_stale = False
        self._data_farm_degraded = False
        # State transition (anything → connected) — stamp at the mutation
        # site so ``health()`` stays a pure read.
        self._last_event_ms = now_ms_utc()
        logger.info(
            "[STEP 3/3] IBKR connected: account=%s is_paper=%s server_version=%s",
            account_id,
            is_paper,
            self._ib.client.serverVersion() if self._ib.client else None,
        )
        return self.health()

    async def disconnect(self) -> None:
        """Idempotent disconnect."""
        was_connected = self._ib.isConnected()
        if was_connected:
            # ib_async.IB only exposes a synchronous disconnect(); there is
            # no disconnectAsync. The smoke run on 2026-05-13 surfaced this
            # the first time cmd_start ever called us in production.
            self._ib.disconnect()
        self._connected_account = None
        if was_connected:
            self._last_event_ms = now_ms_utc()

    async def probe(self, *, timeout_s: float = 4.0) -> None:
        """Bounded app-level liveness probe.

        TCP keep-alive catches dead sockets eventually; this verifies that
        TWS/Gateway is still processing API requests. ``reqCurrentTimeAsync``
        is preferred because it is cheap and does not consume market-data
        lines. If the ib_async surface changes, the exception is intentionally
        surfaced to the monitor, which will force a reconnect.
        """
        self.require_live()
        try:
            await asyncio.wait_for(self._ib.reqCurrentTimeAsync(), timeout=timeout_s)
        except Exception as exc:
            self._last_probe_error = f"{type(exc).__name__}: {exc}"
            self._record_broker_event(
                "BROKER_PROBE_FAILED", probe_error=self._last_probe_error
            )
            raise
        self._last_probe_ms = now_ms_utc()
        self._last_probe_error = None
        self._record_broker_event("BROKER_PROBE_OK", probe_ts_ms=self._last_probe_ms)

    def mark_recovery_succeeded(self) -> None:
        self._subscriptions_stale = False
        self._last_recovery_ms = now_ms_utc()
        self._recovery_error = None
        self._record_broker_event(
            "BROKER_RECOVERY_OK", recovery_ts_ms=self._last_recovery_ms
        )

    def mark_recovery_failed(self, exc: Exception) -> None:
        self._recovery_error = f"{type(exc).__name__}: {exc}"
        self._last_event_ms = now_ms_utc()
        self._record_broker_event(
            "BROKER_RECOVERY_FAILED", recovery_error=self._recovery_error
        )

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

    @property
    def last_ibkr_code(self) -> int | None:
        return self._last_ibkr_code

    @property
    def subscriptions_stale(self) -> bool:
        return self._subscriptions_stale

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
        """Client-observable snapshot. Pure read — no side effects.

        Returns the wire model with the client's own view stamped in:
        ``connection_state`` is one of {connected, soft_lost,
        disconnected}; the monitor's "reconnecting" overlay is applied
        by ``build_broker_health(client, monitor)`` — the single place
        that knows both halves of the state machine. Monitor-only fields
        default (``reconnect_attempt=None``, ``successful_reconnect_count=0``).
        """
        connected = self.is_connected()
        sv: int | None = None
        if connected and self._ib.client is not None:
            try:
                sv = int(self._ib.client.serverVersion())
            except Exception:
                sv = None
        return IbkrConnectionHealth(
            mode=self._settings.mode,
            host=self._settings.host,
            port=self._settings.port,
            client_id=self._settings.client_id,
            connected=connected,
            account_id=self._connected_account,
            is_paper=(_is_paper_account(self._connected_account) if self._connected_account else None),
            server_version=sv,
            fetched_at_ms=now_ms_utc(),
            connection_state=self.connection_state,
            recovery_state=recovery_state_from_connection_state(self.connection_state),
            connection_lost=self._connection_lost,
            connectivity_lost_count=self._connectivity_lost_count,
            last_ibkr_code=self._last_ibkr_code,
            last_ibkr_message=self._last_ibkr_message,
            subscriptions_stale=self._subscriptions_stale,
            data_farm_degraded=self._data_farm_degraded,
            last_probe_ms=self._last_probe_ms,
            last_probe_error=self._last_probe_error,
            last_recovery_ms=self._last_recovery_ms,
            recovery_error=self._recovery_error,
            last_transition_ms=self._last_event_ms,
            broker_event_log_write_failed_count=self._broker_event_log_write_failed_count,
            last_broker_event_log_write_failed_at_ms=self._last_broker_event_log_write_failed_at_ms,
        )

    @property
    def connection_state(self) -> ClientConnectionState:
        """Cockpit-facing state, *from the client's perspective only*.
        The monitor's "reconnecting" overlay is applied by
        ``build_broker_health``, not here."""
        if not self.is_connected():
            return "disconnected"
        if self._connection_lost:
            return "soft_lost"
        if self._subscriptions_stale:
            return "subscriptions_stale"
        if self._data_farm_degraded:
            return "degraded_data_farm"
        return "connected"

    @property
    def last_event_ms(self) -> int:
        """Wall-clock when the client's observable connection state last
        flipped (connect / disconnect / 1100 / 1101). Read by
        ``build_broker_health`` so the wire-level ``last_transition_ms``
        can be the max of this and the monitor's own transition stamp."""
        return self._last_event_ms

    @property
    def desired_connected(self) -> bool:
        """Whether the operator (or startup contract) wants a live
        connection. The AutoReconnectMonitor short-circuits its tick when
        this is False, so a manual ``/disconnect`` makes the operator's
        "off" sticky."""
        return self._desired_connected

    def set_desired_connected(self, value: bool) -> None:
        """Set by the FastAPI lifespan (based on ``IBKR_CONNECT_ON_STARTUP``)
        and by the ``/connect`` / ``/disconnect`` / ``/reconnect`` router
        endpoints. NOT touched by the monitor — the monitor reads it as
        precondition, never writes it."""
        self._desired_connected = value


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
