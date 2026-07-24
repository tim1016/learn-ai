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
from threading import Lock
from time import monotonic
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
from app.broker.alpaca.config import (
    BROKER_ID,
    AlpacaSettings,
    alpaca_configuration_error_detail,
    get_alpaca_settings,
)
from app.broker.alpaca.errors import map_api_error, status_of
from app.broker.capture.journal import CaptureJournal, get_capture_journal
from app.broker.contract.errors import BrokerAuthError, BrokerUnavailable

_DEFAULT_TIMEOUT_S = 15.0
_DEFAULT_CONNECT_TIMEOUT_S = 3.05
_MAX_IN_FLIGHT_SYNC_CALLS = 8
_SUBMIT_VISIBILITY_GRACE_S = 30.0


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
        self._thread_limiter: anyio.CapacityLimiter | None = None
        self._uncertain_submission_lock = Lock()
        self._uncertain_submissions: dict[str, float] = {}

    def _mark_submission_uncertain(self, client_order_id: str) -> None:
        """Keep lookup absence non-terminal during Alpaca's visibility window."""
        if not client_order_id:
            return
        with self._uncertain_submission_lock:
            self._uncertain_submissions[client_order_id] = (
                monotonic() + _SUBMIT_VISIBILITY_GRACE_S
            )

    def _clear_uncertain_submission(self, client_order_id: str) -> None:
        with self._uncertain_submission_lock:
            self._uncertain_submissions.pop(client_order_id, None)

    def _submission_may_become_visible(self, client_order_id: str) -> bool:
        with self._uncertain_submission_lock:
            deadline = self._uncertain_submissions.get(client_order_id)
            if deadline is None:
                return False
            if monotonic() < deadline:
                return True
            self._uncertain_submissions.pop(client_order_id, None)
            return False

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
        if self._thread_limiter is None:
            # AnyIO 3 requires an active async backend when constructing a
            # limiter. Build it lazily so sync dependency-injection fixtures
            # can still create the broker client.
            self._thread_limiter = anyio.CapacityLimiter(_MAX_IN_FLIGHT_SYNC_CALLS)

        try:
            client = self._client()
        except ValidationError as exc:
            raise BrokerAuthError(
                "Alpaca is not configured — set paper credentials in .env.",
                broker=self.broker_id,
                detail=alpaca_configuration_error_detail(exc),
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
                    # A timed-out worker cannot be force-killed. Keep
                    # abandoned Alpaca workers off AnyIO's process-wide
                    # limiter while their request-level timeout winds down.
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

    # ── Write methods (phase 2) ─────────────────────────────────────────────

    async def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """POST one order to ``/v2/orders`` over the owned capturing session.

        ``order`` is the exact JSON body Alpaca expects
        (``symbol``, ``qty``, ``side``, ``type``, ``time_in_force``,
        ``client_order_id``). The low-level ``post`` drives the same
        ``requests.Session`` the read path uses, so the capture hook journals
        the raw response verbatim, and the same timeout + ``map_api_error``
        translation applies. Returns the raw Alpaca order payload; the adapter
        maps it to a ``BrokerOrder``.
        """
        client_order_id = str(order.get("client_order_id") or "")
        try:
            result = await self._call(
                lambda client: client.post("/orders", order),
                describe="order submission",
            )
        except BrokerUnavailable:
            # The synchronous SDK worker may continue after AnyIO returns a
            # timeout, and Alpaca can accept before its lookup index catches up.
            # Preserve uncertainty through that explicit consistency window.
            self._mark_submission_uncertain(client_order_id)
            raise
        self._clear_uncertain_submission(client_order_id)
        return result

    async def cancel_order(self, order_id: str) -> None:
        """DELETE ``/v2/orders/{order_id}`` over the owned capturing session.

        ``order_id`` is Alpaca's broker-assigned UUID. The low-level ``delete``
        drives the same ``requests.Session`` the read path uses, so the capture
        hook journals the raw response verbatim, and the same timeout +
        ``map_api_error`` translation applies. Alpaca returns HTTP 204 (no body)
        on success — ``delete`` yields ``None`` — and 422 for a non-cancelable
        order, which ``map_api_error`` translates to a typed ``BrokerError``.
        Returns nothing; there is no order payload to map.
        """
        await self._call(
            lambda c: c.delete(f"/orders/{order_id}"), describe="order cancellation"
        )

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> dict[str, Any] | None:
        """GET ``/v2/orders:by_client_order_id`` for the order we minted (S5).

        The SDK's ``get_order_by_client_id(client_id=...)`` hits
        ``GET /orders:by_client_order_id?client_order_id=...`` over the same
        capturing session. In ``raw_data`` mode it returns the parsed order dict
        when the order exists, and raises an ``APIError`` (HTTP 404) when it is
        definitively absent.

        Returns the raw order payload on success (the adapter maps it), or
        ``None`` when Alpaca reports the order absent outside a post-timeout
        visibility window. A 404 inside that window remains
        ``BrokerUnavailable`` because the synchronous submit worker or Alpaca's
        lookup index may still be settling. Outside the window, 404 is
        intercepted **before** ``_call``'s taxonomy would fold it into
        ``BrokerUnavailable``: definitively absent → the submit failed, whereas
        unreachable must stay uncertain. Every other failure (timeout, 5xx,
        network, other 4xx) flows through ``_call`` unchanged.
        """

        def _get(client: Any) -> dict[str, Any] | None:
            try:
                return client.get_order_by_client_id(client_id=client_order_id)
            except APIError as exc:
                if status_of(exc) == 404:
                    if self._submission_may_become_visible(client_order_id):
                        raise BrokerUnavailable(
                            "Alpaca order submission may still become visible.",
                            broker=self.broker_id,
                            detail=(
                                "Alpaca returned 404 inside the post-timeout "
                                "visibility window; the outcome remains uncertain."
                            ),
                        ) from exc
                    return None
                raise

        result = await self._call(_get, describe="order lookup by client_order_id")
        if result is not None:
            self._clear_uncertain_submission(client_order_id)
        return result
