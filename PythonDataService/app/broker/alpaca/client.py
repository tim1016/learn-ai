"""Async wrapper over alpaca-py ``TradingClient`` (Broker System v2, Layer 1).

The service layer is async; alpaca-py is synchronous and drives ``requests``.
This wrapper bridges the two:

- Each SDK call runs in a worker thread via ``anyio.to_thread`` so the event
  loop never blocks.
- The client is built in ``raw_data=True`` mode: it returns the parsed JSON
  (dicts / lists of dicts) rather than SDK models. The adapter is therefore the
  single, explicit ingestion boundary that converts vendor strings to contract
  types (int64 ms UTC, float) — the SDK does no hidden timestamp parsing. The
  SDK still owns auth, URL derivation, request building, and retry, and its
  model definitions remain the schema-drift authority.
- A verbatim-capture hook is installed on the SDK's session, so every response
  is journaled before it is parsed.
- Failures are translated to broker-contract errors; no alpaca-py or requests
  exception escapes this module.

Credentials and the client are built lazily on first call, so registering the
broker at startup never needs keys and the service boots credential-free.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anyio
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, QueryOrderStatus
from alpaca.trading.requests import GetAssetsRequest, GetOrdersRequest
from pydantic import ValidationError
from requests.exceptions import RequestException
from requests.sessions import Session

from app.broker.alpaca.capture_hook import install_capture_hook
from app.broker.alpaca.config import BROKER_ID, AlpacaSettings, get_alpaca_settings
from app.broker.alpaca.errors import map_api_error
from app.broker.capture.journal import CaptureJournal, get_capture_journal
from app.broker.contract.errors import BrokerAuthError, BrokerUnavailable

_DEFAULT_TIMEOUT_S = 15.0
_DEFAULT_CONNECT_TIMEOUT_S = 3.05
_MAX_IN_FLIGHT_SYNC_CALLS = 8


def _config_detail(exc: ValidationError) -> str:
    """Concise reason string from a settings ValidationError (no secrets)."""
    try:
        return "; ".join(str(error.get("msg", "")) for error in exc.errors())
    except Exception:
        return "invalid Alpaca configuration"


def _ms_to_utc(after_ms: int) -> datetime:
    return datetime.fromtimestamp(after_ms / 1000, tz=UTC)


def _install_session_timeout(session: Session, *, timeout_s: float) -> None:
    """Give every SDK request finite connect and read timeouts.

    alpaca-py delegates to ``Session.request`` without a timeout. Wrapping the
    bound method retains an explicit timeout supplied by a future SDK call,
    while making the service default finite for every current call path.
    """
    request = session.request
    timeout = (min(_DEFAULT_CONNECT_TIMEOUT_S, timeout_s), timeout_s)

    def request_with_timeout(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout)
        return request(method, url, **kwargs)

    session.request = request_with_timeout  # type: ignore[method-assign]


class AlpacaTradingClient:
    """Thin async facade over the sync alpaca-py trading client."""

    broker_id = BROKER_ID

    def __init__(
        self,
        *,
        settings: AlpacaSettings | None = None,
        journal: CaptureJournal | None = None,
        client_factory: Callable[[], Any] | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._settings = settings
        self._journal = journal
        self._client_factory = client_factory
        self._timeout_s = timeout_s
        self._raw_client: Any | None = None
        # A timed-out worker cannot be force-killed. Keep abandoned Alpaca
        # workers off AnyIO's process-wide limiter while their request-level
        # timeout lets them wind down.
        self._thread_limiter = anyio.CapacityLimiter(_MAX_IN_FLIGHT_SYNC_CALLS)

    def _build_default_client(self) -> Any:
        settings = self._settings or get_alpaca_settings()
        client = TradingClient(
            api_key=settings.api_key_id,
            secret_key=settings.api_secret_key,
            paper=settings.is_paper,
            raw_data=True,
        )
        _install_session_timeout(client._session, timeout_s=self._timeout_s)
        journal = self._journal or get_capture_journal()
        install_capture_hook(client._session, journal, broker=self.broker_id)
        return client

    def _client(self) -> Any:
        if self._raw_client is None:
            factory = self._client_factory or self._build_default_client
            self._raw_client = factory()
        return self._raw_client

    async def _call(self, fn: Callable[[Any], Any], *, describe: str) -> Any:
        try:
            client = self._client()
        except ValidationError as exc:
            raise BrokerAuthError(
                "Alpaca is not configured — set paper credentials in .env.",
                broker=self.broker_id,
                detail=_config_detail(exc),
            ) from exc

        try:
            with anyio.fail_after(self._timeout_s):
                return await anyio.to_thread.run_sync(
                    fn,
                    client,
                    # AnyIO 3's cancellation option; retained by AnyIO 4 as
                    # a compatibility alias. It lets fail_after return while
                    # a stuck synchronous SDK worker winds down.
                    cancellable=True,
                    limiter=self._thread_limiter,
                )
        except TimeoutError as exc:
            raise BrokerUnavailable(
                f"Alpaca timed out while fetching {describe}.",
                broker=self.broker_id,
                detail=f"The broker did not respond within {self._timeout_s:g} seconds.",
            ) from exc
        except APIError as exc:
            raise map_api_error(exc, broker=self.broker_id) from exc
        except RequestException as exc:
            raise BrokerUnavailable(
                f"Could not reach Alpaca while fetching {describe}.",
                broker=self.broker_id,
                detail=str(exc),
            ) from exc

    # ── Raw read methods (return parsed JSON; the adapter maps to contract) ──

    async def get_account(self) -> dict[str, Any]:
        return await self._call(lambda c: c.get_account(), describe="account")

    async def list_positions(self) -> list[dict[str, Any]]:
        return await self._call(lambda c: c.get_all_positions(), describe="positions")

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if status is not None:
            kwargs["status"] = QueryOrderStatus(status)
        if limit is not None:
            kwargs["limit"] = limit
        if after_ms is not None:
            kwargs["after"] = _ms_to_utc(after_ms)
        request = GetOrdersRequest(**kwargs)
        return await self._call(lambda c: c.get_orders(filter=request), describe="orders")

    async def list_activities(
        self,
        *,
        limit: int,
        page_token: str | None = None,
    ) -> list[dict[str, Any]]:
        # Alpaca's ``after`` parameter filters a different vendor timestamp
        # than the contract's ``occurred_at_ms``. Fetch a bounded page here;
        # the broker filters mapped contract records by occurred_at_ms below.
        params = {"page_size": limit, "direction": "desc"}
        if page_token is not None:
            params["page_token"] = page_token
        return await self._call(
            lambda c: c.get("/account/activities", data=params),
            describe="activities",
        )

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if status is not None:
            kwargs["status"] = AssetStatus(status)
        request = GetAssetsRequest(**kwargs)
        payloads = await self._call(
            lambda c: c.get_all_assets(filter=request), describe="assets"
        )
        # Alpaca's assets endpoint has no limit/pagination parameter. Cap at
        # the SDK boundary so the adapter never maps an unbounded response.
        return payloads[:limit]

    async def get_clock(self) -> dict[str, Any]:
        return await self._call(lambda c: c.get_clock(), describe="clock")
