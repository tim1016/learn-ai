"""Durable cash reservations for accepted ENTERs (ADR 0059 D4, schema v13).

A reservation is a sibling row committed inside ``ENTER_ACCEPTED``'s own
transaction — product evidence, never a hashed custody fact. These tests drive
it through ``accept_enter`` and read it back through the repository, and pin
the two properties the rest of the envelope depends on: adding a reservation
changes no ``custody_transitions.row_hash`` (plan R9), and reserved cash
prices only the part of an ENTER the named observation cannot already see.

An ENTER is reserved by passing the envelope gate, never a hand-built
reservation: what gets written is whatever ``require_envelope_admission``
admitted, so these tests exercise the same seam production does.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    AccountObservation,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.sqlite import enter as enter_module
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.custody_schema_contract import (
    HOLDS_COMPATIBILITY_VIEW_DDL,
)
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_order_acknowledgement,
    fold_order_evidence,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    HOLD_REASON_CODE_SQL_PARAMS,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _TestClock

ACCOUNT_ID = "PA-ENVELOPE"
SID = "spy-bot"
SID_B = "qqq-bot"
RUN_ID = "run-1"
RUN_ID_B = "run-2"

T0 = 1_788_040_000_000  # a fixed int64 ms UTC; every stamp is repo.clock()
# The trailing-fill timeline: terminal ack, then the cash observation, then the
# websocket execution slice that observation cannot possibly have seen.
T1_TERMINAL_ACK = T0 + 1_000
T2_OBSERVATION = T0 + 2_000
T3_TRAILING_FILL = T0 + 3_000

ENVELOPE_VALUES = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)

_CAUSE = LossHoldCause(
    day_start_ms=1_788_000_000_000,
    day_pnl_usd=-5_250.0,
    loss_limit_usd=5_000.0,
    last_equity_usd=100_000.0,
    observed_at_ms=T0,
)

# The ``holds`` view a pre-ADR-0059 build baked into its file: same shape, two
# codes instead of three. Derived from the current DDL rather than transcribed,
# so it cannot silently stop being "the current view minus the loss hold".
_V12_HOLDS_VIEW_DDL = HOLDS_COMPATIBILITY_VIEW_DDL.replace(
    f"'{LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE}', ", ""
)


@pytest.fixture
def clock() -> _TestClock:
    return _clock_at(T0)


@pytest.fixture
def repo(tmp_path: Path, clock: _TestClock) -> Iterator[ClerkSqliteRepository]:
    clerk = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    yield clerk
    clerk.close()


@pytest.fixture
def active_instance(repo: ClerkSqliteRepository, clock: _TestClock) -> tuple[str, str]:
    _register_active(repo, clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
    return SID, RUN_ID


@pytest.fixture
def two_active_instances(
    repo: ClerkSqliteRepository, clock: _TestClock
) -> tuple[tuple[str, str], tuple[str, str]]:
    _register_active(repo, clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
    _register_active(repo, clock, strategy_instance_id=SID_B, symbol="QQQ", run_id=RUN_ID_B)
    return (SID, RUN_ID), (SID_B, RUN_ID_B)


def _register_active(
    repo: ClerkSqliteRepository,
    clock: _TestClock,
    *,
    strategy_instance_id: str,
    symbol: str,
    run_id: str,
) -> None:
    """Register one instance and start its run — every stamp from ``clock``."""
    repo.register_strategy_instance(
        strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="h1"
    )
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=run_id,
        clock=clock,
    )


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _gate(*, cash: float = 100_000.0) -> LiveEnvelopeGate:
    """A gate holding one observation fresh at ``T0`` — enough cash to admit."""
    gate = LiveEnvelopeGate(values=ENVELOPE_VALUES, custody_is_simulated=True)
    gate.publish(
        AccountObservation(
            observed_at_ms=T0,
            broker_cash_usd=cash,
            cash_available_usd=cash,
            last_equity_usd=cash,
            unrealized_pl_usd=0.0,
            position_count=0,
        )
    )
    return gate


def _observed_order(
    client_order_id: str,
    *,
    status: str,
    filled_quantity: float,
    filled_avg_price: float | None,
    source_event_at_ms: int,
) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=f"bo-{client_order_id}",
        client_order_id=client_order_id,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=10.0,
        filled_quantity=filled_quantity,
        limit_price=None,
        stop_price=None,
        filled_avg_price=filled_avg_price,
        status=status,
        submitted_at_ms=T0,
        created_at_ms=T0,
        updated_at_ms=source_event_at_ms,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=source_event_at_ms,
    )


def _db_path(artifacts_root: Path) -> Path:
    return artifacts_root / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db"


def _rewind_to_v12(db_path: Path) -> None:
    """Make a real v13 file look like the v12 file a prior build left behind.

    Both halves matter: no ``envelope_reservations`` table, and a ``holds``
    view whose *stored* SQL still names only the two v12 codes — which is the
    shape that would project a loss hold as an uncertainty.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            "DROP TABLE envelope_reservations;\n"
            "DROP VIEW holds;\n"
            f"{_V12_HOLDS_VIEW_DDL}"
            "UPDATE control_meta SET schema_version = 12 WHERE id = 1;\n"
        )
        conn.commit()
    finally:
        conn.close()


def test_a_fresh_authority_has_the_reservations_table_at_schema_v13(
    repo: ClerkSqliteRepository,
) -> None:
    assert schema.SCHEMA_VERSION == 13
    assert repo.control_meta_snapshot().schema_version == 13
    assert (
        repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='envelope_reservations'"
        ).fetchone()
        is not None
    )


def test_a_v12_authority_migrates_additively_to_v13(tmp_path: Path, clock: _TestClock) -> None:
    """The upgrade adds the table and re-publishes the view from live code.

    A view definition is stored text, baked in at the version that created it,
    so a v12 file's ``holds`` still names two codes while a fresh v13 file
    names three. Re-rendering it in the migration is what makes an upgraded
    file and a fresh one project the loss hold identically.
    """
    assert _V12_HOLDS_VIEW_DDL != HOLDS_COMPATIBILITY_VIEW_DDL

    clerk = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    _register_active(clerk, clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
    accept_enter(
        clerk,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="d1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=10),
    )
    before = [
        tuple(row)
        for row in clerk._conn.execute(
            "SELECT sequence, row_hash FROM custody_transitions ORDER BY sequence"
        )
    ]
    assert before
    clerk.close()
    _rewind_to_v12(_db_path(tmp_path))

    reopened = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert reopened.control_meta_snapshot().schema_version == 13
        assert (
            reopened._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='envelope_reservations'"
            ).fetchone()
            is not None
        )
        assert [
            tuple(row)
            for row in reopened._conn.execute(
                "SELECT sequence, row_hash FROM custody_transitions ORDER BY sequence"
            )
        ] == before

        view_sql = reopened._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'holds'"
        ).fetchone()["sql"]
        for reason_code in HOLD_REASON_CODE_SQL_PARAMS:
            assert f"'{reason_code}'" in view_sql

        assert (
            raise_account_hold(
                reopened,
                reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                evidence_refs=[f"day-pnl:{_CAUSE.day_start_ms}"],
                cause_facts=_CAUSE.to_mapping(),
            )
            == "raised"
        )
        assert [
            (row["reason_code"], row["state"])
            for row in reopened._conn.execute("SELECT reason_code, state FROM holds")
        ] == [(LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, "ACTIVE")]
    finally:
        reopened.close()


def test_accepting_an_enter_with_a_reservation_writes_the_row_in_the_same_commit(
    repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    accepted = accept_enter(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    row = repo._conn.execute(
        "SELECT quantity, reference_price, reserved_at_ms FROM envelope_reservations "
        "WHERE effect_operation_id = ?",
        (accepted.effect_operation_id,),
    ).fetchone()
    assert tuple(row) == (10.0, 100.0, T0)


def test_the_reservation_never_enters_the_hash_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan R9: a reservation is a sibling row, so it cannot move a row hash."""
    monkeypatch.setattr(enter_module, "mint_intent_id", lambda: "intent-1")

    row_hashes: list[str] = []
    for name, envelope in (("with", _gate()), ("without", None)):
        clock = _clock_at(T0)
        clerk = ClerkSqliteRepository.initialize(
            account_id=ACCOUNT_ID, artifacts_root=tmp_path / name, clock=clock
        )
        try:
            _register_active(clerk, clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
            accept_enter(
                clerk,
                account_id=ACCOUNT_ID,
                strategy_instance_id=SID,
                decision_id="d1",
                lifecycle_run_id=RUN_ID,
                leg=_leg(quantity=10),
                envelope=envelope,
                reference_price=100.0,
            )
            row_hashes.append(
                clerk._conn.execute(
                    "SELECT row_hash FROM custody_transitions "
                    "WHERE transition_kind = 'ENTER_ACCEPTED'"
                ).fetchone()["row_hash"]
            )
        finally:
            clerk.close()

    assert row_hashes[0] == row_hashes[1]


@pytest.mark.parametrize(
    ("broker_state", "fills", "observed_at_ms", "expected"),
    [
        (None, [], T0, 1_000.0),  # working, unacked: full
        ("new", [(4, T0 - 1)], T0, 600.0),  # 4 filled before the observation: remainder
        ("new", [(4, T0 + 1)], T0, 1_000.0),  # filled after: cash cannot reflect it yet
        ("filled", [(10, T0 - 1)], T0, 0.0),  # done and observed
        ("filled", [(10, T0 + 1)], T0, 1_000.0),  # done, not yet observed
        ("canceled", [], T0, 0.0),  # dead, nothing to reserve
        ("canceled", [(3, T0 + 1)], T0, 300.0),  # dead with a fill after the observation
    ],
)
def test_reserved_cash_prices_only_what_the_observation_cannot_see(
    repo: ClerkSqliteRepository,
    clock: _TestClock,
    active_instance: tuple[str, str],
    broker_state: str | None,
    fills: list[tuple[float, int]],
    observed_at_ms: int,
    expected: float,
) -> None:
    sid, run_id = active_instance
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None

    # The "filled before the observation" cases wind the fixture clock back
    # past the acceptance for brevity; that is harmless here because
    # ``reserved_cash_usd`` never reads ``reserved_at_ms`` — only the fill's
    # ``recorded_at_ms`` against the observation.
    if fills:
        for index, (cumulative_qty, recorded_at_ms) in enumerate(fills, start=1):
            clock.value = recorded_at_ms
            fold_order_evidence(
                repo,
                effect_operation_id=accepted.effect_operation_id,
                order=_observed_order(
                    accepted.order_ref,
                    status=broker_state or "new",
                    filled_quantity=float(cumulative_qty),
                    filled_avg_price=100.0,
                    source_event_at_ms=T0 + index,
                ),
            )
    elif broker_state is not None:
        clock.value = T0
        fold_order_evidence(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            order=_observed_order(
                accepted.order_ref,
                status=broker_state,
                filled_quantity=0.0,
                filled_avg_price=None,
                source_event_at_ms=T0 + 1,
            ),
        )

    assert repo.reserved_cash_usd(observed_at_ms=observed_at_ms) == pytest.approx(expected)


def _refuse_coverage_conflict() -> TransitionInput:
    raise AssertionError("a first exact execution has nothing to conflict with")


def test_a_trailing_websocket_fill_on_a_terminal_order_is_still_reserved(
    repo: ClerkSqliteRepository,
    clock: _TestClock,
    active_instance: tuple[str, str],
) -> None:
    """A terminal order's ``updated_at_ms`` cannot bound its unobserved fills.

    ``SqliteTradeUpdateEvidenceSink.record_lifecycle_event`` folds a websocket
    frame as ``EXECUTION_SLICE_FILLED`` — which writes a ``fills`` row and
    touches nothing on ``orders`` — and then calls
    ``fold_order_acknowledgement(append_stale_ack=False)``, which appends
    nothing when the snapshot has not moved. So an order that went terminal at
    T1 can record a fill at T3 with ``orders.updated_at_ms`` still at T1, and
    an observation taken at T2 in between has seen neither. Pricing that fill
    is the whole point of the reservation.
    """
    sid, run_id = active_instance
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None

    clock.value = T1_TERMINAL_ACK
    fold_order_acknowledgement(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        order=_observed_order(
            accepted.order_ref,
            status="filled",
            filled_quantity=10.0,
            filled_avg_price=100.0,
            source_event_at_ms=T1_TERMINAL_ACK,
        ),
        append_stale_ack=False,
    )
    order_before = repo._conn.execute(
        "SELECT broker_state, updated_at_ms FROM orders WHERE order_ref = ?",
        (accepted.order_ref,),
    ).fetchone()
    assert (order_before["broker_state"], order_before["updated_at_ms"]) == (
        "filled",
        T1_TERMINAL_ACK,
    )

    clock.value = T3_TRAILING_FILL
    facts = ExecutionSliceFilledFacts(
        execution_id="exec-trailing-1",
        symbol="SPY",
        side="BUY",
        slice_qty=10.0,
        slice_price=100.0,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=T3_TRAILING_FILL,
    )
    assert (
        repo.append_execution_slice_if_absent(
            execution_id=facts.execution_id,
            order_ref=accepted.order_ref,
            build_transition=lambda: TransitionInput(
                strategy_instance_id=accepted.command.strategy_instance_id,
                run_id=accepted.command.run_id,
                command_id=accepted.command.command_id,
                effect_operation_id=accepted.effect_operation_id,
                order_ref=accepted.order_ref,
                transition_kind="EXECUTION_SLICE_FILLED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="in_progress",
                source_event_at_ms=facts.source_event_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code="EXECUTION_SLICE_FILLED",
                facts_json=facts.to_facts_json(),
            ),
            build_coverage_conflict=_refuse_coverage_conflict,
        )
        == "appended"
    )

    # The order row is exactly what it was before the fill: this is why a
    # prefilter on ``updated_at_ms`` dropped the reservation entirely.
    order_after = repo._conn.execute(
        "SELECT broker_state, updated_at_ms FROM orders WHERE order_ref = ?",
        (accepted.order_ref,),
    ).fetchone()
    assert (order_after["broker_state"], order_after["updated_at_ms"]) == (
        "filled",
        T1_TERMINAL_ACK,
    )
    assert order_after["updated_at_ms"] < T2_OBSERVATION <= T3_TRAILING_FILL

    assert repo.reserved_cash_usd(observed_at_ms=T2_OBSERVATION) == pytest.approx(1_000.0)


def test_reservations_sum_across_instances(
    repo: ClerkSqliteRepository, two_active_instances: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (sid_a, run_a), (sid_b, run_b) = two_active_instances
    gate = _gate()
    for sid, run_id, symbol, quantity in ((sid_a, run_a, "SPY", 6), (sid_b, run_b, "QQQ", 4)):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=sid,
            decision_id="d1",
            lifecycle_run_id=run_id,
            leg=_leg(symbol=symbol, quantity=quantity),
            envelope=gate,
            reference_price=100.0,
        )

    assert repo.reserved_cash_usd(observed_at_ms=T0) == pytest.approx(1_000.0)
