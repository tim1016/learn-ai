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
from datetime import UTC, datetime
from pathlib import Path

from app.broker.ibkr.config import IbkrSettings, get_settings
from app.broker.ibkr.models import IbkrConnectionHealth

logger = logging.getLogger(__name__)


# Sentinel value for ``IBKR_HOST`` that triggers default-gateway detection.
# The container's default gateway is, by container-runtime convention, the
# host machine — this is the most reliable way to reach the Windows host
# from inside Podman, where ``host.docker.internal`` does not work the
# same way it does under Docker Desktop.
HOST_AUTO_SENTINEL = "auto"


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
    """If the configured host is the AUTO sentinel, resolve to the gateway.

    Falls back to the literal sentinel string on detection failure so the
    underlying ``ib_async.connectAsync`` produces a clear DNS-style error
    rather than this helper silently returning a wrong host.
    """
    if configured != HOST_AUTO_SENTINEL:
        return configured
    detected = _detect_host_gateway()
    if detected is None:
        logger.error(
            "IBKR_HOST=auto but default gateway could not be detected from "
            "/proc/net/route. Falling back to the literal 'auto', which will "
            "fail at the wire — set IBKR_HOST explicitly in your .env."
        )
        return configured
    logger.info("IBKR_HOST=auto resolved to default gateway %s", detected)
    return detected


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
                    readonly=False,
                )
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "IBKR connect attempt %d failed: %s",
                    attempt,
                    exc,
                )
                await asyncio.sleep(min(2.0 * attempt, 5.0))
        else:
            raise BrokerError(
                f"IBKR connect failed after {s.connect_attempts} attempts; "
                f"last error: {last_error!r}"
            ) from last_error

        # ── sentinel check ──────────────────────────────────────────────
        accounts = list(self._ib.managedAccounts())
        if not accounts:
            await self._ib.disconnectAsync()
            raise BrokerError("Connected to IBKR but managedAccounts() returned empty.")

        # Single-account assumption holds for individual paper/live setups.
        # Multi-account FA structures will need explicit selection — out of
        # scope for Phase 1, but we surface the constraint clearly.
        if len(accounts) > 1:
            logger.warning(
                "[STEP 2/3] IBKR returned %d managed accounts; using the first (%s). "
                "Multi-account selection is a Phase 2 follow-up.",
                len(accounts),
                accounts[0],
            )
        account_id = accounts[0]
        is_paper = _is_paper_account(account_id)

        if s.mode == "paper" and not is_paper:
            await self._ib.disconnectAsync()
            raise ConnectionRefusedDueToSentinelError(
                f"IBKR_MODE=paper but connected account {account_id!r} is NOT a paper "
                f"account (paper IDs begin with 'DU'). Disconnected. Refusing to proceed."
            )
        if s.mode == "live" and is_paper:
            await self._ib.disconnectAsync()
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
            await self._ib.disconnectAsync()
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

    def require_connected(self) -> None:
        if not self.is_connected():
            raise NotConnectedError("IBKR client is not connected.")

    def health(self) -> IbkrConnectionHealth:
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
            is_paper=(
                _is_paper_account(self._connected_account)
                if self._connected_account
                else None
            ),
            server_version=sv,
            fetched_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )


# ── module-level singleton ──────────────────────────────────────────────
# Instantiated by the FastAPI lifespan event in ``app.main``. Tests can
# replace via ``set_client`` without going through the lifespan.

_client: IbkrClient | None = None


def get_client() -> IbkrClient:
    if _client is None:
        raise NotConnectedError(
            "IbkrClient is not initialised. The FastAPI lifespan event "
            "should construct one at startup."
        )
    return _client


def set_client(client: IbkrClient | None) -> None:
    """Install the process-wide client. Called from the lifespan event."""
    global _client
    _client = client
