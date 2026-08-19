"""Public startup-selection tests for the one-authority Alpaca runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import select_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.activation import (
    ActivationRecord,
    ActivationRecordInvalid,
)
from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    GuardedBrokerReadPort,
    GuardedBrokerTradePort,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseHeld,
)
from app.broker.contract.models import BrokerAccountSnapshot


def _account() -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id="PA-TEST",
        account_mode="paper",
        account_status="ACTIVE",
        currency="USD",
        cash=1_000.0,
        equity=1_000.0,
        buying_power=2_000.0,
        portfolio_value=1_000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1,
        observed_at_ms=2,
    )


def _activation() -> ActivationRecord:
    return ActivationRecord.create(
        account_id="PA-TEST",
        authority_generation=1,
        db_identity_token="db-token",
        broker_proof_reference="proof.json",
        broker_proof_sha256="0" * 64,
        legacy_quarantine_manifest="quarantine.json",
        legacy_quarantine_manifest_sha256="1" * 64,
        activated_at_ms=1,
    )


class _Broker:
    broker_id = "alpaca"

    async def get_account(self) -> BrokerAccountSnapshot:
        return _account()

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []

    async def list_activities(self, **_kwargs: Any) -> list:
        return []

    async def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("startup must not submit")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("startup must not cancel")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None


class _ActivationStore:
    def __init__(
        self,
        record: object | None,
        *,
        invalid: bool = False,
        resolve_invalid: bool = False,
    ) -> None:
        self.record = record
        self.invalid = invalid
        self.resolve_invalid = resolve_invalid
        self.resolved: tuple[str, int, str, Path] | None = None

    def latest(self, _account_id: str) -> object | None:
        if self.invalid:
            raise ActivationRecordInvalid("tampered activation")
        return self.record

    def resolve(
        self,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        artifacts_root: Path,
    ) -> object:
        if self.resolve_invalid:
            raise ActivationRecordInvalid("activation does not match SQLite identity")
        self.resolved = (
            account_id,
            authority_generation,
            db_identity_token,
            artifacts_root,
        )
        assert self.record is not None
        return self.record


async def test_no_activation_installs_no_custody_authority(tmp_path: Path) -> None:
    broker = _Broker()
    opened = False

    def _open(_account_id: str, _root: Path) -> ClerkSqliteRepository:
        nonlocal opened
        opened = True
        raise AssertionError("missing activation must not open writable SQLite")

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(None),
        repository_opener=_open,
    )

    assert runtime.authority_kind == "unavailable"
    assert runtime.clerk is None
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "ACTIVATION_REQUIRED"
    assert runtime.startup_failure.activation_detected is False
    assert not opened


async def test_invalid_activation_installs_no_mutating_clerk(tmp_path: Path) -> None:
    broker = _Broker()

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(None, invalid=True),
    )

    assert runtime.authority_kind == "unavailable"
    assert runtime.clerk is None
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "ACTIVATION_RECORD_INVALID"


async def test_valid_activation_opens_and_recovers_only_sqlite(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    meta = repo.control_meta_snapshot()
    activation = _activation()
    store = _ActivationStore(activation)

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=store,
        repository_opener=lambda _account_id, _root: repo,
    )

    assert runtime.authority_kind == "sqlite"
    assert runtime.clerk is not None
    assert runtime.clerk.authority_kind == "sqlite"
    assert runtime.startup_failure is None
    assert store.resolved == (
        "PA-TEST",
        meta.authority_generation,
        meta.db_identity_token,
        tmp_path,
    )
    assert runtime.sqlite_repository is repo
    assert isinstance(runtime.clerk._read, GuardedBrokerReadPort)
    assert isinstance(runtime.clerk._trade, GuardedBrokerTradePort)
    assert runtime.sweep is not None
    assert runtime.sweep._read is runtime.clerk._read
    assert runtime.sweep._trade is runtime.clerk._trade
    assert runtime.evidence_sink is not None
    assert runtime.evidence_sink.intake is runtime.clerk.intake
    await runtime.close()


async def test_lease_heartbeat_runs_before_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The execution-lease heartbeat must be live during startup recovery.

    A clean-account boot whose recovery only reads from the broker performs
    no writes, so nothing renews the 30s lease. If the heartbeat only started
    after boot recovery, a slow recovery could let the lease expire before the
    sweep ever ran, stranding the authority read-only.
    """
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    heartbeat_live_during_recovery = False

    async def _recover_observes_heartbeat(_self: object) -> None:
        nonlocal heartbeat_live_during_recovery
        heartbeat_live_during_recovery = any(
            task.get_name() == "alpaca-sqlite-execution-lease-heartbeat"
            and not task.done()
            for task in asyncio.all_tasks()
        )

    monkeypatch.setattr(
        "app.broker.alpaca.clerk.active_authority.SqliteAlpacaClerkFacade.recover",
        _recover_observes_heartbeat,
    )

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        repository_opener=lambda _account_id, _root: repo,
    )

    assert heartbeat_live_during_recovery
    assert runtime.authority_kind == "sqlite"
    await runtime.close()


async def test_activated_database_open_failure_installs_no_authority(
    tmp_path: Path,
) -> None:
    broker = _Broker()

    def _fail_open(_account_id: str, _root: Path) -> ClerkSqliteRepository:
        raise OSError("SQLite unavailable")

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        repository_opener=_fail_open,
    )

    assert runtime.authority_kind == "unavailable"
    assert runtime.clerk is None
    assert runtime.sqlite_repository is None
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"
    assert runtime.startup_failure.activation_detected is True
    assert runtime.startup_failure.authority_generation == 1
    assert runtime.startup_failure.db_identity_token == "db-token"


async def test_activated_startup_waits_for_a_crashed_writers_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An immediate restart recovers after the prior process lease expires."""
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    attempts = 0
    expiry_elapsed = False

    async def _expire_after_retry_interval(delay_s: float) -> None:
        nonlocal expiry_elapsed
        assert delay_s == pytest.approx(0.01)
        expiry_elapsed = True

    monkeypatch.setattr(
        "app.broker.alpaca.clerk.active_authority.asyncio.sleep",
        _expire_after_retry_interval,
    )

    def _open_after_expiry(_account_id: str, _root: Path) -> ClerkSqliteRepository:
        nonlocal attempts
        attempts += 1
        if not expiry_elapsed:
            raise ExecutionLeaseHeld("previous process lease has not expired")
        return repo

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        repository_opener=_open_after_expiry,
        execution_lease_wait_timeout_s=0.1,
        execution_lease_retry_interval_s=0.01,
    )

    assert attempts == 2
    assert runtime.authority_kind == "sqlite"
    await runtime.close()


async def test_activated_startup_fails_closed_when_execution_lease_never_expires(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    attempts = 0

    def _lease_held(_account_id: str, _root: Path) -> ClerkSqliteRepository:
        nonlocal attempts
        attempts += 1
        raise ExecutionLeaseHeld("previous process lease remains live")

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        repository_opener=_lease_held,
        execution_lease_wait_timeout_s=0.003,
        execution_lease_retry_interval_s=0.001,
    )

    assert attempts >= 2
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"


async def test_activation_identity_mismatch_installs_no_authority(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(
            _activation(),
            resolve_invalid=True,
        ),
        repository_opener=lambda _account_id, _root: repo,
    )

    assert runtime.authority_kind == "unavailable"
    assert runtime.clerk is None
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "ACTIVATION_RECORD_INVALID"
    assert runtime.startup_failure.activation_detected is True
    assert runtime.startup_failure.authority_generation == 1
    assert runtime.startup_failure.db_identity_token == "db-token"


async def test_activated_recovery_timeout_installs_no_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    async def _never_recovers(_self: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "app.broker.alpaca.clerk.active_authority.SqliteAlpacaClerkFacade.recover",
        _never_recovers,
    )

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        repository_opener=lambda _account_id, _root: repo,
        startup_recovery_timeout_s=0.001,
    )

    assert runtime.authority_kind == "unavailable"
    assert runtime.clerk is None
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "SQLITE_CLERK_STARTUP_FAILED"
