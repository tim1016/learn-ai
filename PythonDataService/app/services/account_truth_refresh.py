"""Account Truth refresh boundary with snapshot-cache side effects."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.broker.ibkr.account_truth import (
    AccountTruthCollectionContext,
    build_account_truth_collection_context,
    fetch_account_truth,
)
from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.config import IbkrSettings, get_settings
from app.broker.ibkr.health import build_broker_health
from app.broker.ibkr.models import IbkrConnectionHealth
from app.schemas.account_truth import AccountTruthResponse
from app.services.account_truth_snapshot import (
    DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS,
    AccountTruthSnapshotProvider,
    get_account_truth_snapshot_provider,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS = 15_000
ACCOUNT_TRUTH_REFRESH_BACKOFF_MAX_MULTIPLIER = 4
ACCOUNT_TRUTH_REFRESH_JITTER_RATIO = 0.15
_ACCOUNT_TRUTH_REFRESH_UNAVAILABLE_STATES = frozenset(
    {
        "disabled",
        "disconnected",
        "hard_down",
        "reconnecting",
        "soft_lost",
    }
)


def validate_account_truth_refresh_cadence(
    interval_ms: int,
    *,
    hard_ttl_ms: int = DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS,
) -> None:
    """Assert refresh cadence has real margin under the readiness TTL."""

    if interval_ms <= 0:
        raise ValueError("Account Truth refresh interval must be positive.")
    if hard_ttl_ms <= 0:
        raise ValueError("Account Truth readiness TTL must be positive.")
    if interval_ms * 2 >= hard_ttl_ms:
        raise ValueError(
            "Account Truth refresh interval must be less than half the readiness TTL "
            f"(interval_ms={interval_ms}, hard_ttl_ms={hard_ttl_ms})."
        )


validate_account_truth_refresh_cadence(DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS)


def _refresh_sleep_seconds(
    interval_ms: int,
    *,
    consecutive_failures: int,
    random_fraction: float | None = None,
) -> float:
    base_s = interval_ms / 1000
    multiplier = min(
        2 ** max(0, consecutive_failures),
        ACCOUNT_TRUTH_REFRESH_BACKOFF_MAX_MULTIPLIER,
    )
    fraction = random.random() if random_fraction is None else random_fraction
    centered = (max(0.0, min(1.0, fraction)) * 2) - 1
    jitter = 1 + (centered * ACCOUNT_TRUTH_REFRESH_JITTER_RATIO)
    return max(0.001, base_s * multiplier * jitter)


def account_truth_refresh_session_unavailable(health: IbkrConnectionHealth) -> bool:
    """Return True only when account/order evidence cannot be refreshed."""

    return (
        health.account_id is None
        or not health.connected
        or health.connection_state in _ACCOUNT_TRUTH_REFRESH_UNAVAILABLE_STATES
    )


def account_truth_artifacts_root(settings: IbkrSettings | None = None) -> Path:
    """Return the account-artifacts root shared by Account Truth callers."""

    active_settings = settings or get_settings()
    return Path(active_settings.live_runs_root).parent


async def refresh_account_truth_now(
    client: IbkrClient,
    *,
    context: str,
    account_id: str | None = None,
    artifacts_root: Path | None = None,
    health: IbkrConnectionHealth | None = None,
    snapshot_provider: AccountTruthSnapshotProvider | None = None,
) -> AccountTruthResponse:
    """Build Account Truth refresh context once and update the readiness cache."""

    health = health if health is not None else build_broker_health(client, get_monitor())
    collection_context = build_account_truth_collection_context(
        artifacts_root=artifacts_root if artifacts_root is not None else account_truth_artifacts_root(),
        account_id=account_id if account_id is not None else health.account_id,
        context=context,
    )
    return await refresh_account_truth_and_update_cache(
        client,
        health=health,
        collection_context=collection_context,
        snapshot_provider=snapshot_provider,
    )


async def refresh_account_truth_and_update_cache(
    client: IbkrClient,
    *,
    health: IbkrConnectionHealth,
    collection_context: AccountTruthCollectionContext,
    snapshot_provider: AccountTruthSnapshotProvider | None = None,
) -> AccountTruthResponse:
    """Fetch Account Truth and keep the readiness cache in sync with the attempt."""

    provider = snapshot_provider or get_account_truth_snapshot_provider()
    try:
        truth = await fetch_account_truth(
            client,
            health=health,
            collection_context=collection_context,
        )
    except BrokerError as exc:
        provider.mark_refresh_failed(
            health.account_id,
            detail=str(exc),
        )
        raise
    provider.remember(truth)
    return truth


class AccountTruthRefreshLoop:
    """Account-scoped background refresh loop for the Account Truth cache."""

    def __init__(
        self,
        *,
        client: IbkrClient,
        artifacts_root: Path | None = None,
        interval_ms: int = DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS,
        snapshot_provider: AccountTruthSnapshotProvider | None = None,
        refresh_now: Callable[..., Awaitable[AccountTruthResponse]] = refresh_account_truth_now,
        account_truth_observer: Callable[[AccountTruthResponse], object] | None = None,
    ) -> None:
        self._client = client
        self._artifacts_root = artifacts_root
        self._interval_ms = interval_ms
        self._snapshot_provider = snapshot_provider or get_account_truth_snapshot_provider()
        validate_account_truth_refresh_cadence(
            interval_ms,
            hard_ttl_ms=self._snapshot_provider.hard_ttl_ms,
        )
        self._refresh_now = refresh_now
        self._account_truth_observer = account_truth_observer
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._last_account_id: str | None = None
        self._last_refresh_result: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the singleton background task. Idempotent."""

        if self.is_running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="account-truth-refresh-loop",
        )

    async def stop(self) -> None:
        """Cancel the background task and wait briefly for shutdown."""

        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.CancelledError, TimeoutError):
            logger.debug("account truth refresh loop stopped")
        finally:
            self._task = None

    async def refresh_once(self) -> AccountTruthResponse | None:
        """Perform one account-scoped refresh attempt."""

        async with self._refresh_lock:
            account_id = self._last_account_id
            attempted_at_ms: int | None = None
            try:
                health = build_broker_health(self._client, get_monitor())
                attempted_at_ms = health.fetched_at_ms
                account_id = health.account_id or self._last_account_id
                if account_id is not None:
                    self._last_account_id = account_id
                if account_truth_refresh_session_unavailable(health):
                    self._mark_refresh_unavailable(
                        account_id,
                        detail=(
                            "Account Truth refresh requires an available account/order broker session; "
                            f"current broker state is {health.connection_state}."
                        ),
                        attempted_at_ms=attempted_at_ms,
                    )
                    self._last_refresh_result = "unavailable"
                    return None

                refresh_kwargs: dict[str, object] = {
                    "context": "account truth refresh loop",
                    "account_id": health.account_id,
                    "health": health,
                    "snapshot_provider": self._snapshot_provider,
                }
                if self._artifacts_root is not None:
                    refresh_kwargs["artifacts_root"] = self._artifacts_root
                result = await self._refresh_now(self._client, **refresh_kwargs)
                if self._account_truth_observer is not None:
                    try:
                        self._account_truth_observer(result)
                    except Exception:
                        logger.exception(
                            "account truth observer failed",
                            extra={"account_id": result.account_id},
                        )
                self._last_refresh_result = "success"
                return result
            except BrokerError as exc:
                self._mark_refresh_unavailable(
                    account_id,
                    detail=str(exc),
                    attempted_at_ms=attempted_at_ms,
                )
                self._last_refresh_result = "failure"
                logger.warning(
                    "account truth refresh failed",
                    extra={"account_id": account_id, "exception": repr(exc)},
                )
                return None
            except Exception as exc:
                self._mark_refresh_unavailable(
                    account_id,
                    detail=f"Account Truth refresh failed unexpectedly: {exc}",
                    attempted_at_ms=attempted_at_ms,
                )
                self._last_refresh_result = "failure"
                logger.exception(
                    "account truth refresh failed unexpectedly",
                    extra={"account_id": account_id},
                )
                return None

    async def _run(self) -> None:
        consecutive_failures = 0
        while not self._stopped.is_set():
            result = await self.refresh_once()
            consecutive_failures = (
                0
                if result is not None or self._last_refresh_result == "unavailable"
                else consecutive_failures + 1
            )
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=_refresh_sleep_seconds(
                        self._interval_ms,
                        consecutive_failures=consecutive_failures,
                    ),
                )
            except TimeoutError:
                continue

    def _mark_refresh_unavailable(
        self,
        account_id: str | None,
        *,
        detail: str,
        attempted_at_ms: int | None = None,
    ) -> None:
        if account_id is None:
            return
        self._snapshot_provider.mark_refresh_failed(
            account_id,
            detail=detail,
            attempted_at_ms=attempted_at_ms if attempted_at_ms is not None else now_ms_utc(),
        )


__all__ = [
    "DEFAULT_ACCOUNT_TRUTH_REFRESH_INTERVAL_MS",
    "AccountTruthRefreshLoop",
    "account_truth_artifacts_root",
    "account_truth_refresh_session_unavailable",
    "refresh_account_truth_and_update_cache",
    "refresh_account_truth_now",
    "validate_account_truth_refresh_cadence",
]
