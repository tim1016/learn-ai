"""SQLite catalog projection coverage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal, cast

import pytest

from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection
from app.schemas.broker_bots import BotStatusView
from app.schemas.live_runs import BotDutyOutcomeView
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
    status_label_for,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import build_sqlite_catalog
from tests.broker.v2panel.fixtures import ACCT, OTHER_SID, SID


def _status(
    *,
    sid: str,
    strategy_key: str = "deployment_validation",
    strategy_label: str | None = "Deployment Validation",
    phase: str = "ON_DUTY",
    running: bool = True,
    desired_state: str = "RUNNING",
    duty_kind: str | None = None,
    mode: Literal["log_only", "dry_run", "trade"] = "log_only",
) -> BotStatusView:
    duty = (
        BotDutyOutcomeView(
            kind=duty_kind, reason_code="X", recorded_at_ms=1, run_id="r1"
        )
        if duty_kind is not None
        else None
    )
    return BotStatusView(
        strategy_instance_id=sid,
        strategy_key=strategy_key,
        strategy_label=strategy_label,
        broker="alpaca",
        symbol="SPY",
        mode=mode,
        quantity=1,
        running=running,
        phase=phase,  # type: ignore[arg-type]
        desired_state=desired_state,  # type: ignore[arg-type]
        active_run_id="r1" if running else None,
        duty_outcome=duty,
        binding_created_at_ms=1,
        last_transition_at_ms=2,
    )


def _economic_snapshot(*, sid: str, control_revision: int = 42) -> EconomicSnapshot:
    return EconomicSnapshot(
        account_id=ACCT,
        strategy_instance_id=sid,
        authority_generation=7,
        control_revision=control_revision,
        session_open_ms=1_700_000_000_000,
        session_close_ms=1_700_023_400_000,
        recent_fills=(),
        fills_today=3,
        exposure={"SPY": 2.0},
        realized_pnl_today=12.5,
        open_pnl=3.25,
        marks_complete=True,
        mark_observed_at_ms={"SPY": 1_700_010_000_000},
        fee_fidelity="reported",
        execution_coverage="complete",
        last_activity_at_ms=1_700_010_000_000,
    )


def _projection(*, sid: str, control_revision: int = 42) -> ClerkProjection:
    return cast(
        ClerkProjection,
        SimpleNamespace(
            account_id=ACCT,
            strategy_instance_id=sid,
            authority_generation=7,
            control_revision=control_revision,
            holds=(),
            uncertainties=(),
            authority_health="healthy",
        ),
    )


def test_status_label_maps_the_closed_vocabulary() -> None:
    assert status_label_for(_status(sid=SID, running=True)) == "Working"
    assert status_label_for(_status(sid=SID, running=False, phase="OFF_DUTY")) == "Off duty"
    assert status_label_for(_status(sid=SID, phase="RETIRED", running=False)) == "Retired"


def test_sqlite_catalog_uses_config_identity_and_one_economic_rollup() -> None:
    status = _status(
        sid=SID, strategy_key="opening_range_breakout", mode="trade"
    ).model_copy(update={"strategy_label": "Opening Range Breakout — Paper"})
    catalog = build_sqlite_catalog(
        [status],
        projections={SID: _projection(sid=SID)},
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
    )

    assert len(catalog) == 1
    row = catalog[0]
    assert row.strategy_key == "opening_range_breakout"
    assert row.strategy_label == "Opening Range Breakout — Paper"
    assert row.exposure == {"SPY": 2.0}
    assert row.fills_today == 3
    assert row.realized_pnl_today == pytest.approx(12.5, abs=1e-6)
    assert row.open_pnl == pytest.approx(3.25, abs=1e-6)
    assert row.last_activity_at_ms == 1_700_010_000_000


def test_sqlite_catalog_refuses_a_registered_bot_without_immutable_config() -> None:
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="immutable SQLite configuration"):
        build_sqlite_catalog(
            [_status(sid=SID, strategy_key="unknown", mode="trade")],
            projections={SID: _projection(sid=SID)},
            economic_rollups={SID: _economic_snapshot(sid=SID)},
            account_id=ACCT,
        )


def test_sqlite_catalog_refuses_a_registered_bot_without_config_display_name() -> None:
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="immutable SQLite configuration"):
        build_sqlite_catalog(
            [
                _status(
                    sid=SID,
                    strategy_key="opening_range_breakout",
                    strategy_label=None,
                    mode="trade",
                )
            ],
            projections={SID: _projection(sid=SID)},
            economic_rollups={SID: _economic_snapshot(sid=SID)},
            account_id=ACCT,
        )


def test_sqlite_catalog_refuses_economics_spanning_authority_revisions() -> None:
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="multiple authority revisions"):
        build_sqlite_catalog(
            [
                _status(sid=SID, strategy_key="opening_range_breakout", mode="trade"),
                _status(sid=OTHER_SID, strategy_key="ema_crossover_signal", mode="trade"),
            ],
            projections={
                SID: _projection(sid=SID, control_revision=42),
                OTHER_SID: _projection(sid=OTHER_SID, control_revision=43),
            },
            economic_rollups={
                SID: _economic_snapshot(sid=SID, control_revision=42),
                OTHER_SID: _economic_snapshot(sid=OTHER_SID, control_revision=43),
            },
            account_id=ACCT,
        )


def test_sqlite_catalog_refuses_custody_and_economics_from_different_revisions() -> None:
    with pytest.raises(SqliteCatalogRevisionMismatch, match="do not share one authority revision"):
        build_sqlite_catalog(
            [_status(sid=SID, strategy_key="opening_range_breakout", mode="trade")],
            projections={SID: _projection(sid=SID, control_revision=41)},
            economic_rollups={SID: _economic_snapshot(sid=SID, control_revision=42)},
            account_id=ACCT,
        )
