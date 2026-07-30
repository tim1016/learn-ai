"""Tests for the catalog projection (S1, spec §5).

Journal-fixture-driven: bootstrap the rollup cache from a synthetic order
journal, compose catalog rows, and assert the closed status vocabulary, the
attention-first sort, and the narrowed desired_state.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.rollup_cache import BotRollupCache
from app.schemas.broker_bots import BotStatusView
from app.schemas.live_runs import BotDutyOutcomeView
from app.services.broker_v2_panel.catalog_projection_service import (
    bootstrap_rollup_cache,
    build_catalog,
    status_label_for,
)
from tests.broker.v2panel.fixtures import ACCT, OTHER_SID, SID, fill_entry


def _status(
    *,
    sid: str,
    phase: str = "ON_DUTY",
    running: bool = True,
    desired_state: str = "RUNNING",
    duty_kind: str | None = None,
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
        broker="alpaca",
        symbol="SPY",
        mode="log_only",
        running=running,
        phase=phase,  # type: ignore[arg-type]
        desired_state=desired_state,  # type: ignore[arg-type]
        active_run_id="r1" if running else None,
        duty_outcome=duty,
        binding_created_at_ms=1,
        last_transition_at_ms=2,
    )


def test_status_label_maps_the_closed_vocabulary() -> None:
    assert status_label_for(_status(sid=SID, running=True)) == "Working"
    assert status_label_for(_status(sid=SID, running=False, phase="OFF_DUTY")) == "Off duty"
    assert status_label_for(_status(sid=SID, phase="RETIRED", running=False)) == "Retired"


def test_catalog_composes_rollups_and_status() -> None:
    entries = [
        fill_entry(sid=SID, intent="i1", ts_ms=1000, qty=100, price=500.0),
    ]
    cache = BotRollupCache()
    bootstrap_rollup_cache(cache, [SID], entries)

    catalog = build_catalog([_status(sid=SID)], cache, account_id=ACCT)

    assert len(catalog) == 1
    row = catalog[0]
    assert row.strategy_instance_id == SID
    assert row.account_id == ACCT
    assert row.status_label == "Working"
    assert row.desired_state == "RUNNING"
    assert row.exposure == {"SPY": 100.0}


def test_catalog_is_attention_first() -> None:
    entries: list = []
    cache = BotRollupCache()
    bootstrap_rollup_cache(cache, [SID, OTHER_SID], entries)

    # OTHER_SID crashed → lifecycle attention; SID is clean.
    statuses = [
        _status(sid=SID, running=True),
        _status(sid=OTHER_SID, running=False, phase="OFF_DUTY", duty_kind="CRASHED"),
    ]
    catalog = build_catalog(statuses, cache, account_id=ACCT)

    assert catalog[0].strategy_instance_id == OTHER_SID
    assert catalog[0].needs_attention is True
    assert catalog[1].needs_attention is False


def test_catalog_no_journal_activity_still_lists_the_bot() -> None:
    cache = BotRollupCache()
    bootstrap_rollup_cache(cache, [SID], [])
    catalog = build_catalog([_status(sid=SID, running=False, phase="OFF_DUTY")], cache, account_id=ACCT)
    assert len(catalog) == 1
    assert catalog[0].fills_today == 0
    assert catalog[0].exposure == {}


def test_catalog_never_emits_paused_desired_state() -> None:
    cache = BotRollupCache()
    bootstrap_rollup_cache(cache, [SID], [])
    # A PAUSED lifecycle value is narrowed to STOPPED by the projection.
    status = _status(sid=SID, running=False, phase="OFF_DUTY", desired_state="PAUSED")
    catalog = build_catalog([status], cache, account_id=ACCT)
    assert catalog[0].desired_state == "STOPPED"
