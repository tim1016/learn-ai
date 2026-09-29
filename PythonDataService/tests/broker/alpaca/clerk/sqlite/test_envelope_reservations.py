"""Durable cash reservations for accepted ENTERs (ADR 0059 D4, schema v13).

A reservation is folded from ``ENTER_ACCEPTED``'s own facts: the exact price
the ENTER was admitted against and the fee provision its entry requirement
recorded (#2553). These tests drive it through ``accept_enter`` and read it
back through the repository, and pin what the rest of the envelope depends
on: reserved cash prices only the part of an ENTER the named observation
cannot already see, and its fee is the recorded provision, never a re-quote.
A row an earlier build wrote beside the transition with no recorded fee still
prices its fills, but refuses instead of claiming a zero fee on any unfilled
remainder.

An ENTER is reserved by passing the envelope gate, never a hand-built
reservation: what gets written is whatever ``require_envelope_admission``
admitted, so these tests exercise the same seam production does.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    AccountObservation,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.budget_authority import (
    ENTRY_FEE_PROVISION_UNRECORDED,
    commit_budget_authority_cutover,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.custody_schema_contract import (
    HOLDS_COMPATIBILITY_VIEW_DDL,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.envelope_reservations import EntryFeeProvisionUnrecorded
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCorrectedFacts,
    ExecutionSliceFilledFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_order_acknowledgement,
    fold_order_evidence,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    RefusalClass,
    classify_admission_refusal,
    raise_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    HOLD_REASON_CODE_SQL_PARAMS,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_ACCOUNT_ID as ACCOUNT_ID,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_RUN_ID as RUN_ID,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_RUN_ID_B,
    ENVELOPE_SID_B,
    _clock_at,
    _register_active,
    _TestClock,
    complete_fee_evidence,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_SID as SID,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_T0 as T0,
)

# The trailing-fill timeline: terminal ack, then the cash observation, then the
# websocket execution slice that observation cannot possibly have seen.
T1_TERMINAL_ACK = T0 + 1_000
T2_OBSERVATION = T0 + 2_000
T3_TRAILING_FILL = T0 + 3_000


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


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _gate(*, cash: float = 100_000.0) -> LiveEnvelopeGate:
    """A gate holding one observation fresh at ``T0`` — enough cash to admit."""
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True)
    gate.publish(
        AccountObservation(
            observed_at_ms=T0,
            broker_cash_usd=cash,
            cash_available_usd=cash,
            equity_usd=cash,
            last_equity_usd=cash,
            position_count=0,
            risk_cash_flow_evidence_complete=True,
            risk_cash_flow_window_start_ms=day_pnl_window_start_ms(T0),
            risk_equity_window_start_ms=day_pnl_window_start_ms(T0),
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
            "DROP TABLE deployment_budgets;\n"
            "DROP TABLE account_risk_policy;\n"
            "DROP TABLE envelope_reservations;\n"
            "DROP TRIGGER trg_budget_authority_monotonic;\n"
            "ALTER TABLE control_meta DROP COLUMN authorization_version;\n"
            "DROP VIEW holds;\n"
            f"{_V12_HOLDS_VIEW_DDL}"
            "UPDATE control_meta SET schema_version = 12 WHERE id = 1;\n"
        )
        conn.commit()
    finally:
        conn.close()


def test_a_fresh_authority_has_the_reservations_table_at_schema_v13(
    envelope_repo: ClerkSqliteRepository,
) -> None:
    assert schema.SCHEMA_VERSION >= 20
    assert envelope_repo.control_meta_snapshot().schema_version == schema.SCHEMA_VERSION
    assert (
        envelope_repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='envelope_reservations'"
        ).fetchone()
        is not None
    )


def test_a_v12_authority_migrates_additively_to_v13(tmp_path: Path, envelope_clock: _TestClock) -> None:
    """The upgrade adds the table and re-publishes the view from live code.

    A view definition is stored text, baked in at the version that created it,
    so a v12 file's ``holds`` still names two codes while a fresh v13 file
    names three. Re-rendering it in the migration is what makes an upgraded
    file and a fresh one project the loss hold identically.
    """
    assert _V12_HOLDS_VIEW_DDL != HOLDS_COMPATIBILITY_VIEW_DDL

    clerk = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=envelope_clock
    )
    _register_active(clerk, envelope_clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
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
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=envelope_clock
    )
    try:
        assert reopened.control_meta_snapshot().schema_version == schema.SCHEMA_VERSION
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


def test_accepting_an_enter_records_its_price_and_fee_provision_in_the_same_commit(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    """#2553: an ENTER admitted before the budget cutover records its fee too.

    The provision is the entry requirement's own fee -- 10 shares of CAT,
    rounded up to the cent -- carried by ``ENTER_ACCEPTED``'s facts, so the
    claim is that recorded cent and never a zero.
    """
    sid, run_id = active_instance
    accepted = accept_enter(
        envelope_repo,
        account_id=envelope_repo.account_id,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    row = envelope_repo._conn.execute(
        "SELECT quantity, exact_reference_price, fee_provision_cents, reserved_at_ms "
        "FROM envelope_reservations WHERE effect_operation_id = ?",
        (accepted.effect_operation_id,),
    ).fetchone()
    assert (row["quantity"], Decimal(row["exact_reference_price"]), row["fee_provision_cents"], row["reserved_at_ms"]) == (
        10.0, Decimal(100), 1, T0,
    )
    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T0) == Decimal("1000.01")


@pytest.mark.parametrize(
    ("broker_state", "fills", "seen_before_ms", "expected"),
    # Every unfilled remainder also claims its share of the recorded one-cent
    # fee provision, rounded up to the cent; no remainder claims no fee.
    [
        (None, [], T0, "1000.01"),  # working, unacked: full
        ("new", [(4, T0 - 1)], T0, "600.01"),  # 4 filled before the observation: remainder
        ("new", [(4, T0 + 1)], T0, "1000.01"),  # filled after: cash cannot reflect it yet
        ("filled", [(10, T0 - 1)], T0, "0"),  # done and observed
        ("filled", [(10, T0 + 1)], T0, "1000"),  # done, not yet observed
        ("filled", [], T0, "1000.01"),  # filled at submit, fill not yet recorded (the shadow case): whole notional
        ("filled", [(4, T0 - 1)], T0, "600.01"),  # filled, 4 recorded before the observation, rest unrecorded: remainder
        ("canceled", [], T0, "0"),  # dead, nothing to reserve
        ("canceled", [(3, T0 + 1)], T0, "300"),  # dead with a fill after the observation
        ("expired", [], T0, "0"),  # dead, nothing recorded: nothing
        ("rejected", [], T0, "0"),  # dead: nothing
        ("replaced", [], T0, "0"),  # dead: the fourth state, pinned like its siblings
    ],
)
def test_reserved_cash_prices_only_what_the_observation_cannot_see(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    active_instance: tuple[str, str],
    broker_state: str | None,
    fills: list[tuple[float, int]],
    seen_before_ms: int,
    expected: str,
) -> None:
    sid, run_id = active_instance
    accepted = accept_enter(
        envelope_repo,
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
    # ``reserved_cash_decimal`` never reads ``reserved_at_ms`` — only the
    # fill's ``recorded_at_ms`` against the observation.
    if fills:
        for index, (cumulative_qty, recorded_at_ms) in enumerate(fills, start=1):
            envelope_clock.value = recorded_at_ms
            fold_order_evidence(
                envelope_repo,
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
        envelope_clock.value = T0
        fold_order_evidence(
            envelope_repo,
            effect_operation_id=accepted.effect_operation_id,
            order=_observed_order(
                accepted.order_ref,
                status=broker_state,
                filled_quantity=0.0,
                filled_avg_price=None,
                source_event_at_ms=T0 + 1,
            ),
        )

    assert envelope_repo.reserved_cash_decimal(seen_before_ms=seen_before_ms) == Decimal(expected)


def _refuse_coverage_conflict() -> TransitionInput:
    raise AssertionError("a first exact execution has nothing to conflict with")


def test_a_trailing_websocket_fill_on_a_terminal_order_is_still_reserved(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
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
        envelope_repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None

    envelope_clock.value = T1_TERMINAL_ACK
    fold_order_acknowledgement(
        envelope_repo,
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
    order_before = envelope_repo._conn.execute(
        "SELECT broker_state, updated_at_ms FROM orders WHERE order_ref = ?",
        (accepted.order_ref,),
    ).fetchone()
    assert (order_before["broker_state"], order_before["updated_at_ms"]) == (
        "filled",
        T1_TERMINAL_ACK,
    )

    envelope_clock.value = T3_TRAILING_FILL
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
        envelope_repo.append_execution_slice_if_absent(
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
                clerk_observed_at_ms=envelope_repo.clock(),
                summary_code="EXECUTION_SLICE_FILLED",
                facts_json=facts.to_facts_json(),
            ),
            build_coverage_conflict=_refuse_coverage_conflict,
        )
        == "appended"
    )

    # The order row is exactly what it was before the fill: this is why a
    # prefilter on ``updated_at_ms`` dropped the reservation entirely.
    order_after = envelope_repo._conn.execute(
        "SELECT broker_state, updated_at_ms FROM orders WHERE order_ref = ?",
        (accepted.order_ref,),
    ).fetchone()
    assert (order_after["broker_state"], order_after["updated_at_ms"]) == (
        "filled",
        T1_TERMINAL_ACK,
    )
    assert order_after["updated_at_ms"] < T2_OBSERVATION <= T3_TRAILING_FILL

    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T2_OBSERVATION) == Decimal(1_000)


def _append_slice(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    quantity: float,
    source_event_at_ms: int,
    price: float = 100.0,
    fee: float | None = None,
) -> None:
    """One websocket execution slice with a broker identity a correction can name."""
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol="SPY",
        side="BUY",
        slice_qty=quantity,
        slice_price=price,
        fee=fee,
        fee_fidelity="reported" if fee is not None else "not_reported",
        evidence_source="websocket",
        source_event_at_ms=source_event_at_ms,
    )
    assert (
        repo.append_execution_slice_if_absent(
            execution_id=execution_id,
            order_ref=accepted.order_ref or "",
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
                source_event_at_ms=source_event_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code="EXECUTION_SLICE_FILLED",
                facts_json=facts.to_facts_json(),
            ),
            build_coverage_conflict=_refuse_coverage_conflict,
        )
        == "appended"
    )


def _append_correction(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    superseded_execution_ref: str,
    quantity: float,
    source_event_at_ms: int,
) -> None:
    """Restate a prior slice's quantity, leaving the superseded row auditable."""
    facts = ExecutionCorrectedFacts(
        execution_id=execution_id,
        superseded_execution_ref=superseded_execution_ref,
        symbol="SPY",
        side="BUY",
        corrected_qty=quantity,
        corrected_price=100.0,
        why="Broker restated the execution quantity",
    )
    assert (
        repo.append_execution_correction_or_raise(
            correction=TransitionInput(
                strategy_instance_id=accepted.command.strategy_instance_id,
                run_id=accepted.command.run_id,
                command_id=accepted.command.command_id,
                effect_operation_id=accepted.effect_operation_id,
                order_ref=accepted.order_ref,
                transition_kind="EXECUTION_CORRECTED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="in_progress",
                source_event_at_ms=source_event_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code="EXECUTION_CORRECTED",
                facts_json=facts.to_facts_json(),
            ),
            build_uncertainty=_refuse_correction_uncertainty,
        )
        == "appended"
    )


def _refuse_correction_uncertainty(reason: str) -> TransitionInput:
    raise AssertionError(f"the correction fixture must be valid: {reason}")


@pytest.mark.parametrize(
    ("original_qty", "corrected_qty", "expected"),
    [
        (10.0, 5.0, "500.01"),  # downward: the restated 5 units are unfilled cash again, with their fee share
        (5.0, 10.0, "0"),  # upward: the whole ENTER is filled, nothing left to reserve
    ],
)
def test_a_corrected_fill_reserves_at_its_restated_size(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    active_instance: tuple[str, str],
    original_qty: float,
    corrected_qty: float,
    expected: str,
) -> None:
    """A correction is dated by the root execution, not by its own arrival.

    The original fill is recorded *before* the observation and restated
    *after* it — the case that used to price the remainder at the superseded
    size for the whole life of the working order. What the broker's cash
    reflected at ``T2_OBSERVATION`` is the restated quantity, because the
    execution itself happened at ``T1_TERMINAL_ACK``; only the Clerk's
    knowledge of it arrived late.
    """
    sid, run_id = active_instance
    accepted = accept_enter(
        envelope_repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None

    envelope_clock.value = T1_TERMINAL_ACK
    _append_slice(
        envelope_repo,
        accepted,
        execution_id="exec-original-1",
        quantity=original_qty,
        source_event_at_ms=T1_TERMINAL_ACK,
    )
    envelope_clock.value = T3_TRAILING_FILL
    _append_correction(
        envelope_repo,
        accepted,
        execution_id="exec-corrected-1",
        superseded_execution_ref="exec-original-1",
        quantity=corrected_qty,
        source_event_at_ms=T3_TRAILING_FILL,
    )

    # The order never acknowledged, so it is still working: its unfilled
    # remainder is what the bound has to price.
    assert (
        envelope_repo._conn.execute(
            "SELECT broker_state FROM orders WHERE order_ref = ?", (accepted.order_ref,)
        ).fetchone()["broker_state"]
        is None
    )
    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T2_OBSERVATION) == Decimal(expected)


def test_an_unseen_recorded_fill_reserves_at_its_actual_cost(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    active_instance: tuple[str, str],
) -> None:
    """The #2442 acceptance case: admitted at a 100 close, filled at 101.

    A recorded fill the observation cannot see is cash the broker already
    took at the fill's own price, so the reservation carries the actual cost
    — fill price x quantity plus the reported fee — not the decision-bar
    close the ENTER was admitted against, until the next observation that can
    see the fill supersedes it.
    """
    sid, run_id = active_instance
    accepted = accept_enter(
        envelope_repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None

    envelope_clock.value = T3_TRAILING_FILL
    _append_slice(
        envelope_repo,
        accepted,
        execution_id="exec-filled-at-101",
        quantity=10.0,
        price=101.0,
        fee=1.25,
        source_event_at_ms=T3_TRAILING_FILL,
    )

    # The observation at T2 cannot see a fill recorded at T3: reserve the
    # 1,011.25 the fill actually cost, not the 1,000 the close estimated.
    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T2_OBSERVATION) == Decimal("1011.25")

    # The next broker read supersedes it: once the fill counts as seen, the
    # filled order reserves nothing.
    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T3_TRAILING_FILL + 1) == 0


def test_a_working_order_blends_actual_fill_cost_with_the_decision_price(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    active_instance: tuple[str, str],
) -> None:
    """Only quantity with no recorded fill prices at the reference price.

    A partially filled working order's recorded-but-unseen units cost what
    the fill says (4 x 102 + the 0.50 fee); the units no execution names yet
    have no price but the decision close the ENTER was admitted against.
    """
    sid, run_id = active_instance
    accepted = accept_enter(
        envelope_repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None

    envelope_clock.value = T3_TRAILING_FILL
    _append_slice(
        envelope_repo,
        accepted,
        execution_id="exec-partial-at-102",
        quantity=4.0,
        price=102.0,
        fee=0.50,
        source_event_at_ms=T3_TRAILING_FILL,
    )

    # 4 x 102 + the 0.50 fee, then 6 x 100 plus the unfilled 6/10 of the
    # recorded one-cent provision, rounded up to the cent.
    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T2_OBSERVATION) == Decimal("1008.51")


def test_a_dead_order_prices_its_unseen_fill_at_cost(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    active_instance: tuple[str, str],
) -> None:
    """A canceled order's post-observation fill is spent cash at the fill price.

    Same shape as the parametrized dead-order case above, at a fill price the
    reference price does not predict: the recorded fill is actual spend, not
    an estimate.
    """
    sid, run_id = active_instance
    accepted = accept_enter(
        envelope_repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="d1",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
        envelope=_gate(),
        reference_price=100.0,
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None

    envelope_clock.value = T3_TRAILING_FILL
    fold_order_evidence(
        envelope_repo,
        effect_operation_id=accepted.effect_operation_id,
        order=_observed_order(
            accepted.order_ref,
            status="canceled",
            filled_quantity=3.0,
            filled_avg_price=101.0,
            source_event_at_ms=T3_TRAILING_FILL,
        ),
    )

    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T2_OBSERVATION) == Decimal(303)


def test_reservations_sum_across_instances(
    envelope_repo: ClerkSqliteRepository, two_active_instances: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (sid_a, run_a), (sid_b, run_b) = two_active_instances
    gate = _gate()
    for sid, run_id, symbol, quantity in ((sid_a, run_a, "SPY", 6), (sid_b, run_b, "QQQ", 4)):
        accept_enter(
            envelope_repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=sid,
            decision_id="d1",
            lifecycle_run_id=run_id,
            leg=_leg(symbol=symbol, quantity=quantity),
            envelope=gate,
            reference_price=100.0,
        )

    # Each ENTER's notional plus its own recorded one-cent provision.
    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T0) == Decimal("1000.02")


def test_a_mirror_rebuild_restores_the_recorded_reservation_and_replays_older_entries(tmp_path: Path) -> None:
    """Replay-compatible (#2553): the reservation folds back from ``ENTER_ACCEPTED``'s facts.

    An ENTER recorded without a reservation (here, one admitted with no
    envelope) keeps its exact bytes: the rebuilt chain has the same row
    hashes, and it still reserves nothing.
    """
    clock = _clock_at(T0)
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    try:
        complete_fee_evidence(repo)
        _register_active(repo, clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
        _register_active(repo, clock, strategy_instance_id=ENVELOPE_SID_B, symbol="QQQ", run_id=ENVELOPE_RUN_ID_B)
        accept_enter(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="d1", lifecycle_run_id=RUN_ID,
            leg=_leg(quantity=10), envelope=_gate(), reference_price=100.0,
        )
        accept_enter(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=ENVELOPE_SID_B, decision_id="d1",
            lifecycle_run_id=ENVELOPE_RUN_ID_B, leg=_leg(symbol="QQQ", quantity=10),
        )
        chain = [(row["sequence"], row["row_hash"]) for row in repo.custody_transitions()]
        assert repo.reserved_cash_decimal(seen_before_ms=T0) == Decimal("1000.01")
        database = repo.db_path
    finally:
        repo.close()
    database.rename(database.with_suffix(".saved"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    try:
        assert [(row["sequence"], row["row_hash"]) for row in rebuilt.custody_transitions()] == chain
        assert rebuilt.reserved_cash_decimal(seen_before_ms=T0) == Decimal("1000.01")
    finally:
        rebuilt.close()


def _legacy_enter(repo: ClerkSqliteRepository, sid: str, run_id: str) -> EnterSubmission:
    """An ENTER whose reservation an earlier build wrote beside the transition (#2553).

    That build priced 10 SPY at a float 100 and recorded no fee provision,
    outside the hashed facts. Nothing writes this shape any more, but a store
    written before the change can still hold one.
    """
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=sid,
        decision_id="legacy",
        lifecycle_run_id=run_id,
        leg=_leg(quantity=10),
    )
    with repo.write_fence() as conn:
        conn.execute(
            "INSERT INTO envelope_reservations (effect_operation_id, quantity, reference_price, reserved_at_ms) "
            "VALUES (?, 10, 100.0, ?)",
            (accepted.effect_operation_id, T0),
        )
    return accepted


def test_a_legacy_reservation_with_an_unfilled_remainder_refuses_instead_of_claiming_no_fee(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    two_active_instances: tuple[tuple[str, str], tuple[str, str]],
) -> None:
    """Regression (#2553): an unknown fee is refused under its own code, never priced at zero."""
    (sid_a, run_a), (sid_b, run_b) = two_active_instances
    legacy = _legacy_enter(envelope_repo, sid_a, run_a)
    assert legacy.effect_operation_id is not None and legacy.order_ref is not None

    with pytest.raises(EntryFeeProvisionUnrecorded):
        envelope_repo.reserved_cash_decimal(seen_before_ms=T0)
    before = envelope_repo.control_meta_snapshot().control_revision
    with pytest.raises(AdmissionBlockedError) as exc_info:
        accept_enter(
            envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b, decision_id="d1",
            lifecycle_run_id=run_b, leg=_leg(symbol="QQQ", quantity=1), envelope=_gate(), reference_price=100.0,
        )
    assert exc_info.value.decision.reason_code == ENTRY_FEE_PROVISION_UNRECORDED
    # Account-scoped and self-ending: the refused ENTER retries on the next decision clock.
    assert classify_admission_refusal(ENTRY_FEE_PROVISION_UNRECORDED) is RefusalClass.TRANSIENT
    assert envelope_repo.control_meta_snapshot().control_revision == before
    # Home still places the bot among those holding money.
    assert envelope_repo.bots_holding_money() == frozenset({sid_a})

    # The order ends: its unrecorded remainder is cancelled quantity, never cash.
    envelope_clock.value = T1_TERMINAL_ACK
    fold_order_evidence(
        envelope_repo,
        effect_operation_id=legacy.effect_operation_id,
        order=_observed_order(
            legacy.order_ref, status="canceled", filled_quantity=0.0, filled_avg_price=None,
            source_event_at_ms=T1_TERMINAL_ACK,
        ),
    )
    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T0) == 0
    assert envelope_repo.bots_holding_money() == frozenset()
    assert accept_enter(
        envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=sid_b, decision_id="d2",
        lifecycle_run_id=run_b, leg=_leg(symbol="QQQ", quantity=1), envelope=_gate(), reference_price=100.0,
    ).created


def test_a_legacy_reservation_with_no_remainder_still_claims_its_unseen_fills_at_cost(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    active_instance: tuple[str, str],
) -> None:
    """Only the unknown fee refuses: a fully filled legacy ENTER still reads (replay-compatible)."""
    sid, run_id = active_instance
    legacy = _legacy_enter(envelope_repo, sid, run_id)

    envelope_clock.value = T3_TRAILING_FILL
    _append_slice(
        envelope_repo, legacy, execution_id="legacy-fill", quantity=10.0, price=101.0, fee=1.25,
        source_event_at_ms=T3_TRAILING_FILL,
    )

    assert envelope_repo.reserved_cash_decimal(seen_before_ms=T2_OBSERVATION) == Decimal("1011.25")


def test_a_legacy_reservation_that_outlives_the_budget_cutover_withholds_budget_money(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    """A pre-cutover working ENTER is retained through the cutover; its unknown fee refuses budgets."""
    sid, run_id = active_instance
    _legacy_enter(envelope_repo, sid, run_id)
    submit_stop_run(
        envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, lifecycle_run_id=run_id,
        clock=envelope_repo.clock,
    )
    commit_budget_authority_cutover(envelope_repo, actor="owner", reviewed_token="reviewed", stop_receipt="stopped")

    with pytest.raises(EntryFeeProvisionUnrecorded, match="no recorded fee"):
        envelope_repo.account_budget(cash=100_000, seen_before_ms=T0)
