"""Boot-time selection and process registry for one active Alpaca Clerk.

The broker account is resolved before any writer is constructed.  A missing
activation record selects legacy JSONL; a valid record selects SQLite; an
invalid record or activated-startup failure selects neither.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.broker.alpaca.clerk.active_protocol import ActiveAlpacaClerk
from app.broker.alpaca.clerk.sqlite.activation import (
    ActivationRecord,
    ActivationRecordInvalid,
    ActivationStore,
)
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import (
    ReconciliationSweep as SqliteReconciliationSweep,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.sweep import ReconciliationSweep as LegacyReconciliationSweep
from app.broker.alpaca.clerk.trade_evidence import (
    LegacyLifecycleRecorder,
    LegacyTradeUpdateEvidenceSink,
    SqliteTradeUpdateEvidenceSink,
    TradeUpdateEvidenceSink,
)
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

AuthorityKind = Literal["legacy", "sqlite", "unavailable"]
DEFAULT_STARTUP_RECOVERY_TIMEOUT_S = 60.0


class BackgroundSweep(Protocol):
    def start(self) -> None: ...

    async def stop(self) -> None: ...


class LegacyAlpacaClerk(ActiveAlpacaClerk, LegacyLifecycleRecorder, Protocol):
    pass


class ActivationResolver(Protocol):
    def latest(self, account_id: str) -> ActivationRecord | None: ...

    def resolve(
        self,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        artifacts_root: Path,
    ) -> ActivationRecord | None: ...


@dataclass(frozen=True)
class ClerkStartupFailure:
    """Fail-closed account impact exposed when no mutating Clerk is installed."""

    reason_code: str
    account_id: str | None
    scope: Literal["ACCOUNT_CLERK"]
    impact: str
    recovery: str
    observed_at_ms: int
    activation_detected: bool = False
    authority_generation: int | None = None
    db_identity_token: str | None = None


@dataclass
class ActiveClerkRuntime:
    """Everything main.py needs after one authority selection."""

    authority_kind: AuthorityKind
    clerk: ActiveAlpacaClerk | None = None
    sweep: BackgroundSweep | None = None
    evidence_sink: TradeUpdateEvidenceSink | None = None
    startup_failure: ClerkStartupFailure | None = None
    _sqlite_repository: ClerkSqliteRepository | None = None

    @property
    def sqlite_repository(self) -> ClerkSqliteRepository | None:
        """Return the active SQLite read authority, never a latent database."""
        if self.authority_kind != "sqlite":
            return None
        return self._sqlite_repository

    async def close(self) -> None:
        if self.sweep is not None:
            await self.sweep.stop()
        if isinstance(self.clerk, SqliteAlpacaClerkFacade):
            await self.clerk.drain_effects()
        if self._sqlite_repository is not None:
            self._sqlite_repository.close()
            self._sqlite_repository = None


def _open_repository(account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
    return ClerkSqliteRepository.open(
        account_id=account_id,
        artifacts_root=artifacts_root,
    )


async def select_active_clerk_runtime(
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    artifacts_root: Path,
    legacy_factory: Callable[[], LegacyAlpacaClerk],
    activation_store: ActivationResolver | None = None,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository] = _open_repository,
    startup_recovery_timeout_s: float = DEFAULT_STARTUP_RECOVERY_TIMEOUT_S,
    stream_health_gate: StreamHealthGate | None = None,
) -> ActiveClerkRuntime:
    """Resolve the account, validate activation, and construct one authority."""
    try:
        account = await read.get_account()
    except Exception as exc:
        logger.warning(
            "Alpaca account identity could not be resolved; Clerk unavailable",
            extra={"action": "active_clerk_account_resolution_failed"},
            exc_info=True,
        )
        return _unavailable(
            "BROKER_ACCOUNT_UNAVAILABLE",
            account_id=None,
            recovery=f"Restore the Alpaca account identity probe: {exc}",
        )
    if account.account_mode != "paper":
        return _unavailable(
            "LIVE_ACCOUNT_REFUSED",
            account_id=account.account_id,
            recovery="Use the separately supervised paper-account cutover workflow.",
        )

    store = activation_store or ActivationStore(artifacts_root / "accounts" / "alpaca")
    try:
        activation = store.latest(account.account_id)
    except ActivationRecordInvalid as exc:
        return _unavailable(
            "ACTIVATION_RECORD_INVALID",
            account_id=account.account_id,
            recovery=str(exc),
            activation_detected=True,
        )

    if activation is None:
        try:
            legacy = legacy_factory()
            await asyncio.wait_for(
                legacy.recover(),
                timeout=startup_recovery_timeout_s,
            )
        except Exception as exc:
            logger.warning(
                "Legacy Alpaca Clerk recovery failed; Clerk unavailable",
                extra={"action": "legacy_active_clerk_recovery_failed"},
                exc_info=True,
            )
            return _unavailable(
                "LEGACY_CLERK_RECOVERY_FAILED",
                account_id=account.account_id,
                recovery=str(exc),
            )
        return ActiveClerkRuntime(
            authority_kind="legacy",
            clerk=legacy,
            sweep=LegacyReconciliationSweep(clerk=legacy),
            evidence_sink=LegacyTradeUpdateEvidenceSink(legacy),
        )

    repository: ClerkSqliteRepository | None = None
    sweep: SqliteReconciliationSweep | None = None
    try:
        repository = repository_opener(account.account_id, artifacts_root)
        meta = repository.control_meta_snapshot()
        resolved = store.resolve(
            account.account_id,
            meta.authority_generation,
            meta.db_identity_token,
            artifacts_root,
        )
        if resolved is None:
            raise ActivationRecordInvalid("activation record disappeared during SQLite startup")
        facade = SqliteAlpacaClerkFacade(
            repo=repository,
            read=read,
            trade=trade,
            stream_health=stream_health_gate,
        )
        # Keep the execution lease alive across the (possibly slow) startup
        # recovery passes. The reconcile loop still starts after boot recovery
        # in main.py, but the lease heartbeat must begin now so a clean-account
        # boot whose recovery only reads from the broker cannot let the lease
        # expire before the sweep is running.
        sweep = SqliteReconciliationSweep(repo=repository, read=read, trade=trade)
        sweep.start_lease_heartbeat()
        await asyncio.wait_for(
            facade.recover(),
            timeout=startup_recovery_timeout_s,
        )
    except Exception as exc:
        if sweep is not None:
            await sweep.stop()
        if repository is not None:
            repository.close()
        logger.warning(
            "Activated SQLite Alpaca Clerk failed startup; no writer installed",
            extra={
                "action": "sqlite_active_clerk_startup_failed",
                "account_id": account.account_id,
            },
            exc_info=True,
        )
        return _unavailable(
            (
                "ACTIVATION_RECORD_INVALID"
                if isinstance(exc, ActivationRecordInvalid)
                else "SQLITE_CLERK_STARTUP_FAILED"
            ),
            account_id=account.account_id,
            recovery=str(exc),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )

    return ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=facade,
        sweep=sweep,
        evidence_sink=SqliteTradeUpdateEvidenceSink(
            repo=repository,
            read=read,
            trade=trade,
            intake=facade.intake,
        ),
        _sqlite_repository=repository,
    )


def _unavailable(
    reason_code: str,
    *,
    account_id: str | None,
    recovery: str,
    activation_detected: bool = False,
    authority_generation: int | None = None,
    db_identity_token: str | None = None,
) -> ActiveClerkRuntime:
    return ActiveClerkRuntime(
        authority_kind="unavailable",
        startup_failure=ClerkStartupFailure(
            reason_code=reason_code,
            account_id=account_id,
            scope="ACCOUNT_CLERK",
            impact="Broker-mutating Alpaca Clerk capability is not installed.",
            recovery=recovery,
            observed_at_ms=now_ms_utc(),
            activation_detected=activation_detected,
            authority_generation=authority_generation,
            db_identity_token=db_identity_token,
        ),
    )


_runtime: ActiveClerkRuntime | None = None


def get_active_clerk_runtime() -> ActiveClerkRuntime | None:
    return _runtime


def set_active_clerk_runtime(runtime: ActiveClerkRuntime | None) -> None:
    global _runtime
    _runtime = runtime


__all__ = [
    "DEFAULT_STARTUP_RECOVERY_TIMEOUT_S",
    "ActiveAlpacaClerk",
    "ActiveClerkRuntime",
    "AuthorityKind",
    "ClerkStartupFailure",
    "get_active_clerk_runtime",
    "select_active_clerk_runtime",
    "set_active_clerk_runtime",
]
