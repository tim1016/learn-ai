"""Tests for the Layer A ``ExecutionMatcher``.

Asserts on the matched-ledger output — never on internal data structures.
A perfect day matches every decision to exactly one execution; missed /
extra / partial / stale fills produce the right half-pairs and flags.
"""

from __future__ import annotations

import logging

from app.engine.live.artifacts import DecisionRow, ExecutionRow
from app.engine.live.divergence.execution_matcher import match_executions


def _decision(*, bar_close_ms: int, intended_action: str, signal: str) -> DecisionRow:
    return DecisionRow(
        bar_close_ms=bar_close_ms,
        signal=signal,
        intended_price=100.0,
        strategy_instance_id="spy-ema:inst-1",
        intended_action=intended_action,
    )


def _execution(*, client_order_id: str, ts_ms: int, fill_quantity: int) -> ExecutionRow:
    return ExecutionRow(
        ts_ms=ts_ms,
        exec_id=f"exec-{client_order_id}",
        perm_id=hash(client_order_id) % 1_000_000,
        client_order_id=client_order_id,
        account_id="DU123",
        symbol="SPY",
        fill_quantity=fill_quantity,
        fill_price=100.0,
        fee=1.0,
    )


def test_perfect_day_every_decision_matched_by_client_order_id() -> None:
    decisions = [
        _decision(bar_close_ms=1000, intended_action="BUY", signal="ENTER"),
        _decision(bar_close_ms=2000, intended_action="SELL", signal="EXIT"),
    ]
    executions = [
        _execution(client_order_id="co-1", ts_ms=1005, fill_quantity=10),
        _execution(client_order_id="co-2", ts_ms=2005, fill_quantity=-10),
    ]
    order_links = {"co-1": 1000, "co-2": 2000}

    ledger = match_executions(
        decisions,
        executions,
        session_window=(0, 10_000),
        order_links=order_links,
    )

    matched = [r for r in ledger if r.decision is not None and r.execution is not None]
    assert len(matched) == 2
    assert all(r.match_basis == "client_order_id" for r in matched)
    assert not [r for r in ledger if r.is_missed or r.is_extra]


def test_decision_with_no_execution_is_missed() -> None:
    decisions = [
        _decision(bar_close_ms=1000, intended_action="BUY", signal="ENTER"),
        _decision(bar_close_ms=1500, intended_action="", signal="HOLD"),
    ]

    ledger = match_executions(
        decisions,
        executions=[],
        session_window=(0, 10_000),
        order_links={},
    )

    missed = [r for r in ledger if r.is_missed]
    assert len(missed) == 1
    assert missed[0].decision is decisions[0]
    # A HOLD bar intends no order, so it is never a missed fill.
    assert all(r.decision.signal != "HOLD" for r in missed)


def test_execution_with_no_decision_is_extra() -> None:
    decisions = [_decision(bar_close_ms=1000, intended_action="BUY", signal="ENTER")]
    executions = [
        _execution(client_order_id="co-1", ts_ms=1005, fill_quantity=10),
        _execution(client_order_id="orphan", ts_ms=3005, fill_quantity=5),
    ]
    order_links = {"co-1": 1000}  # "orphan" links to no decision

    ledger = match_executions(
        decisions,
        executions,
        session_window=(0, 10_000),
        order_links=order_links,
    )

    extra = [r for r in ledger if r.is_extra]
    assert len(extra) == 1
    assert extra[0].execution.client_order_id == "orphan"


def test_fill_before_session_window_is_extra_with_stale_session_flag() -> None:
    decisions = [_decision(bar_close_ms=5000, intended_action="BUY", signal="ENTER")]
    executions = [
        # ts_ms precedes the session window start → a leftover from a prior run.
        _execution(client_order_id="stale-co", ts_ms=200, fill_quantity=10),
    ]

    ledger = match_executions(
        decisions,
        executions,
        session_window=(1000, 10_000),
        order_links={"stale-co": 5000},
    )

    stale = [r for r in ledger if "stale_session" in r.flags]
    assert len(stale) == 1
    assert stale[0].is_extra
    # The stale fill must NOT consume the live decision — that decision is missed.
    assert any(r.is_missed and r.decision.bar_close_ms == 5000 for r in ledger)


def test_one_decision_filled_by_two_executions_is_flagged_partial() -> None:
    decisions = [_decision(bar_close_ms=1000, intended_action="BUY", signal="ENTER")]
    executions = [
        _execution(client_order_id="co-1", ts_ms=1005, fill_quantity=6),
        _execution(client_order_id="co-1", ts_ms=1006, fill_quantity=4),
    ]
    order_links = {"co-1": 1000}

    ledger = match_executions(
        decisions,
        executions,
        session_window=(0, 10_000),
        order_links=order_links,
    )

    partials = [r for r in ledger if "partial" in r.flags]
    assert len(partials) == 2
    assert {p.decision.bar_close_ms for p in partials} == {1000}
    # Both fills attach to the one decision; neither shows up as missed/extra.
    assert not [r for r in ledger if r.is_missed or r.is_extra]


def test_missing_order_link_falls_back_to_composite_key() -> None:
    decisions = [_decision(bar_close_ms=1000, intended_action="BUY", signal="ENTER")]
    executions = [
        # No order_links entry → must match on (bar_close_ms as-of, direction).
        _execution(client_order_id="unlinked", ts_ms=1005, fill_quantity=10),
    ]

    ledger = match_executions(
        decisions,
        executions,
        session_window=(0, 10_000),
        order_links={},
    )

    matched = [r for r in ledger if r.decision is not None and r.execution is not None]
    assert len(matched) == 1
    assert matched[0].match_basis == "composite_key"
    assert not [r for r in ledger if r.is_missed or r.is_extra]


def test_composite_fallback_respects_direction() -> None:
    # A SELL decision must not absorb a buy-side fill via the fallback.
    decisions = [_decision(bar_close_ms=1000, intended_action="SELL", signal="EXIT")]
    executions = [_execution(client_order_id="unlinked", ts_ms=1005, fill_quantity=10)]

    ledger = match_executions(
        decisions,
        executions,
        session_window=(0, 10_000),
        order_links={},
    )

    assert any(r.is_missed for r in ledger)  # SELL decision unfilled
    assert any(r.is_extra for r in ledger)  # buy fill unexplained


def test_shadow_sim_fills_are_filtered_explicitly(caplog) -> None:
    decisions = [_decision(bar_close_ms=1000, intended_action="BUY", signal="ENTER")]
    shadow = ExecutionRow(
        ts_ms=1005,
        exec_id="shadow-1",
        perm_id=42,
        client_order_id="co-1",
        account_id="DU123",
        symbol="SPY",
        fill_quantity=10,
        fill_price=100.0,
        fee=0.0,
        execution_source="shadow_sim",
        source_bar_close_ms=1000,
    )

    with caplog.at_level(logging.INFO):
        ledger = match_executions(
            decisions,
            [shadow],
            session_window=(0, 10_000),
            order_links={"co-1": 1000},
        )

    # Layer A is executing-only: the shadow fill appears nowhere in the ledger.
    assert all(r.execution is not shadow for r in ledger)
    # The BUY decision is therefore missed, not silently satisfied by shadow.
    assert any(r.is_missed for r in ledger)
    # Filtering is explicit, not silent.
    assert any("shadow_sim" in rec.message for rec in caplog.records)
