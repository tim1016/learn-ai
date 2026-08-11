"""Regression coverage for the post-activation Broker V2 roster source."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicSnapshot,
    FillWindowProjection,
    SessionEconomicProjection,
)
from app.broker.alpaca.clerk.sqlite.models import (
    BotConfigResource,
    ControlMetaSnapshot,
    DecisionReceiptResource,
)
from app.lean_sidecar.trading_calendar import SessionWindow
from app.services.broker_v2_panel import panel_data_source, sqlite_panel_source
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
)


class _Repository:
    def control_meta_snapshot(self) -> ControlMetaSnapshot:
        return ControlMetaSnapshot(
            schema_version=1,
            account_id="paper-account",
            db_identity_token="test-identity",
            authority_generation=7,
            control_revision=42,
            created_at_ms=1_700_000_000_000,
            last_open_at_ms=1_700_000_000_000,
        )

    def strategy_instances(self) -> list[dict[str, object]]:
        return [
            {
                "strategy_instance_id": "active-spy",
                "symbol": "SPY",
                "config_hash": "a" * 64,
                "created_at_ms": 1_700_000_000_000,
                "retired_at_ms": None,
            },
            {
                "strategy_instance_id": "retired-qqq",
                "symbol": "QQQ",
                "config_hash": "b" * 64,
                "created_at_ms": 1_600_000_000_000,
                "retired_at_ms": 1_710_000_000_000,
            },
        ]

    def strategy_instance(self, strategy_instance_id: str) -> dict[str, object] | None:
        return next(
            (
                registration
                for registration in self.strategy_instances()
                if registration["strategy_instance_id"] == strategy_instance_id
            ),
            None,
        )

    def active_run(self, strategy_instance_id: str):
        if strategy_instance_id == "active-spy":
            return SimpleNamespace(
                lifecycle_run_id="run-active",
                started_at_ms=1_720_000_000_000,
                stopped_at_ms=None,
            )
        return None

    def bot_config(self, strategy_instance_id: str) -> BotConfigResource | None:
        strategy_key = {
            "active-spy": "opening_range_breakout",
            "retired-qqq": "ema_crossover_signal",
        }.get(strategy_instance_id)
        if strategy_key is None:
            return None
        return BotConfigResource(
            strategy_instance_id=strategy_instance_id,
            strategy_key=strategy_key,
            display_name=strategy_key.replace("_", " ").title(),
            config_json="{}",
            config_hash="a" * 64,
            created_at_ms=1_700_000_000_000,
        )


def test_sqlite_roster_uses_only_activated_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    facade = SimpleNamespace(account_id="paper-account", repository=_Repository())
    monkeypatch.setattr(sqlite_panel_source, "active_sqlite_facade", lambda _broker: facade)

    statuses = sqlite_panel_source.read_sqlite_roster_statuses("alpaca")

    assert statuses is not None
    assert [status.strategy_instance_id for status in statuses] == ["active-spy", "retired-qqq"]
    assert statuses[0].running is True
    assert statuses[0].active_run_id == "run-active"
    assert statuses[0].phase == "ON_DUTY"
    assert statuses[0].strategy_key == "opening_range_breakout"
    assert statuses[0].strategy_key != "unknown"
    assert statuses[0].strategy_label == "Opening Range Breakout"
    assert statuses[0].quantity is None
    assert statuses[1].running is False
    assert statuses[1].phase == "RETIRED"

    status = sqlite_panel_source.read_sqlite_bot_status("alpaca", "active-spy")
    assert status is not None
    assert status.strategy_label == "Opening Range Breakout"


@pytest.mark.asyncio
async def test_catalog_does_not_scan_runner_bindings_after_sqlite_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(panel_data_source, "_validate_account", _resolved_account)
    monkeypatch.setattr(
        panel_data_source,
        "_bot_statuses",
        lambda _broker: pytest.fail("activated catalog scanned file-backed runner bindings"),
    )
    monkeypatch.setattr(
        panel_data_source,
        "_latest_decision",
        lambda *_args: pytest.fail("activated catalog read legacy decision journals"),
    )
    monkeypatch.setattr(
        panel_data_source,
        "get_or_create_owner",
        lambda *_args: pytest.fail("activated catalog used the legacy rollup owner"),
    )
    monkeypatch.setattr(sqlite_panel_source, "read_sqlite_catalog_projections", _empty_projections)
    monkeypatch.setattr(
        sqlite_panel_source,
        "read_sqlite_catalog_economic_rollups",
        _economic_rollups,
    )
    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=_Repository()),
    )

    result = await panel_data_source.get_catalog("alpaca", "paper-account")

    assert [row.strategy_instance_id for row in result] == ["active-spy", "retired-qqq"]


@pytest.mark.asyncio
async def test_activated_catalog_never_scans_large_legacy_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_root = tmp_path / "live_state"
    legacy_root.mkdir()
    # Eight times the production cutover's disposable roster, large enough to
    # catch an accidental directory walk without making temp-tree teardown the
    # dominant cost of this regression.
    for index in range(200):
        (legacy_root / f"disposable-{index:05d}").mkdir()

    scan_calls = 0

    def scan_large_legacy_set(_broker: str) -> list[Path]:
        nonlocal scan_calls
        scan_calls += 1
        return list(legacy_root.iterdir())

    monkeypatch.setattr(panel_data_source, "_validate_account", _resolved_account)
    monkeypatch.setattr(panel_data_source, "_bot_statuses", scan_large_legacy_set)
    monkeypatch.setattr(sqlite_panel_source, "read_sqlite_catalog_projections", _empty_projections)
    monkeypatch.setattr(
        sqlite_panel_source,
        "read_sqlite_catalog_economic_rollups",
        _economic_rollups,
    )
    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=_Repository()),
    )

    for _ in range(20):
        result = await panel_data_source.get_catalog("alpaca", "paper-account")

    assert [row.strategy_instance_id for row in result] == ["active-spy", "retired-qqq"]
    assert scan_calls == 0


async def _resolved_account(_broker: str, account_id: str) -> str:
    return account_id


async def _empty_projections(
    _broker: str,
    account_id: str,
    strategy_instance_ids: list[str],
) -> dict[str, object]:
    return {
        strategy_instance_id: SimpleNamespace(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=7,
            control_revision=42,
            holds=(),
            uncertainties=(),
            authority_health="healthy",
        )
        for strategy_instance_id in strategy_instance_ids
    }


async def _economic_rollups(
    _broker: str,
    account_id: str,
    strategy_instance_ids: list[str],
) -> dict[str, EconomicSnapshot]:
    return _economic_rollup_map(account_id, strategy_instance_ids)


def _economic_rollup_map(
    account_id: str,
    strategy_instance_ids: list[str],
) -> dict[str, EconomicSnapshot]:
    return {
        strategy_instance_id: EconomicSnapshot(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=7,
            control_revision=42,
            session_open_ms=1_700_000_000_000,
            session_close_ms=1_700_023_400_000,
            recent_fills=(),
            fills_today=0,
            exposure={},
            realized_pnl_today=0.0,
            open_pnl=0.0,
            marks_complete=True,
            mark_observed_at_ms={},
            fee_fidelity="not_reported",
            execution_coverage="complete",
            last_activity_at_ms=None,
        )
        for strategy_instance_id in strategy_instance_ids
    }


def test_sqlite_roster_refuses_unknown_identity_after_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(repository, "bot_config", lambda _sid: None)
    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=repository),
    )

    with pytest.raises(SqliteCatalogProjectionUnavailable, match="immutable SQLite configuration"):
        sqlite_panel_source.read_sqlite_roster_statuses("alpaca")


@pytest.mark.asyncio
async def test_catalog_economics_use_one_reader_rollup_for_the_whole_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    facade = SimpleNamespace(account_id="paper-account", repository=repository)
    calls: list[tuple[list[str], SessionWindow | None]] = []
    closed = False

    class _Reader:
        @classmethod
        def from_repository(cls, received_repository: _Repository) -> _Reader:
            assert received_repository is repository
            return cls()

        def catalog_economic_rollup(
            self,
            strategy_instance_ids: list[str],
            *,
            session_window: SessionWindow | None,
        ) -> dict[str, EconomicSnapshot]:
            calls.append((strategy_instance_ids, session_window))
            return _economic_rollup_map("paper-account", strategy_instance_ids)

        def close(self) -> None:
            nonlocal closed
            closed = True

    session = SessionWindow(
        session_date=date(2026, 8, 10),
        open_ms_utc=1_700_000_000_000,
        close_ms_utc=1_700_023_400_000,
    )
    monkeypatch.setattr(sqlite_panel_source, "active_sqlite_facade", lambda _broker: facade)
    monkeypatch.setattr(sqlite_panel_source, "SqliteEconomicProjectionReader", _Reader)
    monkeypatch.setattr(sqlite_panel_source, "current_trading_session_window", lambda _now: session)

    rollups = await sqlite_panel_source.read_sqlite_catalog_economic_rollups(
        "alpaca",
        "paper-account",
        ["active-spy", "retired-qqq"],
    )

    assert rollups is not None
    assert list(rollups) == ["active-spy", "retired-qqq"]
    assert calls == [(["active-spy", "retired-qqq"], session)]
    assert closed is True


@pytest.mark.asyncio
async def test_panel_evidence_retries_status_revision_race_with_verified_zero_economics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    facade = SimpleNamespace(account_id="paper-account", repository=repository)
    snapshot = replace(
        _economic_rollup_map("paper-account", ["active-spy"])["active-spy"],
        session_open_ms=None,
        session_close_ms=None,
    )

    class _CustodyReader:
        @classmethod
        def from_repository(cls, received_repository: _Repository) -> _CustodyReader:
            assert received_repository is repository
            return cls()

        def bot_snapshot(self, strategy_instance_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                account_id="paper-account",
                strategy_instance_id=strategy_instance_id,
                authority_generation=7,
                control_revision=42,
            )

        def close(self) -> None:
            return None

    class _EconomicReader:
        @classmethod
        def from_repository(cls, received_repository: _Repository) -> _EconomicReader:
            assert received_repository is repository
            return cls()

        def bot_session_economic_projection(
            self,
            strategy_instance_id: str,
            *,
            session_window: SessionWindow | None,
        ) -> SessionEconomicProjection:
            assert strategy_instance_id == "active-spy"
            assert session_window is None
            return SessionEconomicProjection(snapshot=snapshot, session_fills=())

        def close(self) -> None:
            return None

    monkeypatch.setattr(sqlite_panel_source, "active_sqlite_facade", lambda _broker: facade)
    monkeypatch.setattr(sqlite_panel_source, "SqliteClerkProjectionReader", _CustodyReader)
    monkeypatch.setattr(sqlite_panel_source, "SqliteEconomicProjectionReader", _EconomicReader)
    monkeypatch.setattr(sqlite_panel_source, "current_trading_session_window", lambda _now: None)
    base_meta = repository.control_meta_snapshot()
    revisions = iter((42, 43, 42, 42))
    monkeypatch.setattr(
        repository,
        "control_meta_snapshot",
        lambda: replace(base_meta, control_revision=next(revisions)),
    )

    evidence = await sqlite_panel_source.read_sqlite_panel_evidence(
        "alpaca",
        "paper-account",
        "active-spy",
        now_ms=1_700_000_000_000,
    )

    assert evidence is not None
    assert evidence.economics.session_fills == ()
    assert evidence.economics.snapshot.fills_today == 0
    assert evidence.economics.snapshot.realized_pnl_today == 0.0
    assert evidence.economics.snapshot.session_open_ms is None
    assert evidence.economics.snapshot.session_close_ms is None


@pytest.mark.asyncio
async def test_catalog_economics_use_verified_zero_outside_nyse_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    snapshot = replace(
        _economic_rollup_map("paper-account", ["active-spy"])["active-spy"],
        session_open_ms=None,
        session_close_ms=None,
    )

    class _Reader:
        @classmethod
        def from_repository(cls, received_repository: _Repository) -> _Reader:
            assert received_repository is repository
            return cls()

        def catalog_economic_rollup(
            self,
            strategy_instance_ids: list[str],
            *,
            session_window: SessionWindow | None,
        ) -> dict[str, EconomicSnapshot]:
            assert session_window is None
            assert strategy_instance_ids == ["active-spy"]
            return {"active-spy": snapshot}

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=repository),
    )
    monkeypatch.setattr(sqlite_panel_source, "SqliteEconomicProjectionReader", _Reader)
    monkeypatch.setattr(sqlite_panel_source, "current_trading_session_window", lambda _now: None)

    rollups = await sqlite_panel_source.read_sqlite_catalog_economic_rollups(
        "alpaca",
        "paper-account",
        ["active-spy"],
    )

    assert rollups is not None
    assert rollups["active-spy"].fills_today == 0
    assert rollups["active-spy"].realized_pnl_today == 0.0
    assert rollups["active-spy"].session_open_ms is None


@pytest.mark.asyncio
async def test_chart_evidence_retries_status_revision_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    fills = FillWindowProjection(
        account_id="paper-account",
        strategy_instance_id="active-spy",
        authority_generation=7,
        control_revision=42,
        fills=(),
    )
    reads = 0

    class _Reader:
        @classmethod
        def from_repository(cls, received_repository: _Repository) -> _Reader:
            assert received_repository is repository
            return cls()

        def bot_fill_window(
            self,
            strategy_instance_id: str,
            *,
            from_ms: int,
            to_ms: int,
        ) -> FillWindowProjection:
            nonlocal reads
            reads += 1
            assert strategy_instance_id == "active-spy"
            assert (from_ms, to_ms) == (1_700_000_000_000, 1_700_086_400_000)
            return fills

        def close(self) -> None:
            return None

    base_meta = repository.control_meta_snapshot()
    revisions = iter((42, 43, 42, 42))
    monkeypatch.setattr(
        repository,
        "control_meta_snapshot",
        lambda: replace(base_meta, control_revision=next(revisions)),
    )
    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=repository),
    )
    monkeypatch.setattr(sqlite_panel_source, "SqliteEconomicProjectionReader", _Reader)

    evidence = await sqlite_panel_source.read_sqlite_chart_evidence(
        "alpaca",
        "paper-account",
        "active-spy",
        from_ms=1_700_000_000_000,
        to_ms=1_700_086_400_000,
    )

    assert evidence is not None
    assert evidence.status.strategy_instance_id == "active-spy"
    assert evidence.fills is fills
    assert reads == 2


@pytest.mark.asyncio
async def test_chart_evidence_refuses_persistent_status_revision_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    fills = FillWindowProjection(
        account_id="paper-account",
        strategy_instance_id="active-spy",
        authority_generation=7,
        control_revision=42,
        fills=(),
    )
    reads = 0

    class _Reader:
        @classmethod
        def from_repository(cls, received_repository: _Repository) -> _Reader:
            assert received_repository is repository
            return cls()

        def bot_fill_window(self, *_args: object, **_kwargs: object) -> FillWindowProjection:
            nonlocal reads
            reads += 1
            return fills

        def close(self) -> None:
            return None

    base_meta = repository.control_meta_snapshot()
    revisions = iter((42, 43, 42, 43, 42, 43))
    monkeypatch.setattr(
        repository,
        "control_meta_snapshot",
        lambda: replace(base_meta, control_revision=next(revisions)),
    )
    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=repository),
    )
    monkeypatch.setattr(sqlite_panel_source, "SqliteEconomicProjectionReader", _Reader)

    with pytest.raises(
        sqlite_panel_source.SqlitePanelEconomicUnavailable,
        match="history identity and execution folds changed",
    ):
        await sqlite_panel_source.read_sqlite_chart_evidence(
            "alpaca",
            "paper-account",
            "active-spy",
            from_ms=1_700_000_000_000,
            to_ms=1_700_086_400_000,
        )

    assert reads == 3


def test_sqlite_decision_receipts_adapt_durable_s1_rows_without_jsonl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = DecisionReceiptResource(
        strategy_instance_id="active-spy",
        seq=3,
        outcome="enter_intent",
        symbol="SPY",
        intent_id="intent-3",
        order_ref=None,
        observed_at_ms=1_700_000_180_000,
        facts_json=(
            '{"bar_ref":"SPY@1700000180000","indicator_snapshot":{"fast":12.0},'
            '"reason_code":"STRATEGY_ENTER"}'
        ),
    )
    repository = SimpleNamespace(
        decision_receipt_tail=lambda **kwargs: [resource],
    )
    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=repository),
    )

    receipts = sqlite_panel_source.read_sqlite_decision_receipts("alpaca", "active-spy")

    assert receipts is not None
    assert [receipt.seq for receipt in receipts] == [3]
    assert receipts[0].bar_ref == "SPY@1700000180000"
    assert receipts[0].reason_code == "STRATEGY_ENTER"
    assert receipts[0].intent_id == "intent-3"


@pytest.mark.asyncio
async def test_catalog_retries_when_status_identity_straddles_a_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    attempts = 0
    expected = []

    def build_after_status_retry(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal attempts
        attempts += 1
        return expected

    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=repository),
    )
    monkeypatch.setattr(sqlite_panel_source, "read_sqlite_catalog_projections", _empty_projections)
    monkeypatch.setattr(sqlite_panel_source, "read_sqlite_catalog_economic_rollups", _economic_rollups)
    monkeypatch.setattr(sqlite_panel_source, "build_sqlite_catalog", build_after_status_retry)
    base_meta = repository.control_meta_snapshot()
    revisions = iter((42, 43, 42, 42))
    monkeypatch.setattr(
        repository,
        "control_meta_snapshot",
        lambda: replace(base_meta, control_revision=next(revisions)),
    )

    catalog = await sqlite_panel_source.read_sqlite_catalog("alpaca", "paper-account")

    assert catalog is expected
    assert attempts == 1


@pytest.mark.asyncio
async def test_catalog_fails_closed_after_bounded_revision_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    attempts = 0

    def always_mismatched(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal attempts
        attempts += 1
        raise SqliteCatalogRevisionMismatch("simulated writer contention")

    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=repository),
    )
    monkeypatch.setattr(sqlite_panel_source, "read_sqlite_catalog_projections", _empty_projections)
    monkeypatch.setattr(sqlite_panel_source, "read_sqlite_catalog_economic_rollups", _economic_rollups)
    monkeypatch.setattr(sqlite_panel_source, "build_sqlite_catalog", always_mismatched)

    with pytest.raises(SqliteCatalogRevisionMismatch, match="simulated writer contention"):
        await sqlite_panel_source.read_sqlite_catalog("alpaca", "paper-account")

    assert attempts == 2
