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
admitted, so these tests exercise the same seam production does. What an
order claims is read back through ``entry_cash_claims``, the one claim query
every money read prices from.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.live_envelope import ENTRY_FEE_PROVISION_UNRECORDED
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.budget_authority import BUDGETS_NOT_SWITCHED_ON
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.custody_schema_contract import (
    HOLDS_COMPATIBILITY_VIEW_DDL,
)
from app.broker.alpaca.clerk.sqlite.enter import (
    EnterSubmission,
    EntrySubmissionRefusal,
    accept_enter,
    resolve_enter_submission,
    submit_accepted_enter,
)
from app.broker.alpaca.clerk.sqlite.envelope_reservations import (
    EntryFeeProvisionUnrecorded,
    entry_cash_claims,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionSliceFilledFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_failed,
    fold_order_acknowledgement,
    fold_order_evidence,
    submit_absence_grace_ms,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    raise_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    HOLD_REASON_CODE_SQL_PARAMS,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
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
    _FakeTradePort,
    _register_active,
    _start_legacy_run,
    _TestClock,
    complete_fee_evidence,
    switch_to_budgets,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_SID as SID,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_T0 as T0,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    envelope_gate as _gate,
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


def _claimed(repo: ClerkSqliteRepository, *, seen_before_ms: int) -> Decimal:
    """What entry orders claim beyond a cash reading that saw fills recorded before ``seen_before_ms``.

    Each claim's parts as ``budget_projection`` adds them to the account's
    ``order_claims``: the unfilled remainder's cost and fee, and the unseen
    fills' actual cost.
    """
    return sum(
        (claim.unfilled_cost + claim.unseen_fill_cost + claim.unfilled_fee
         for claim in entry_cash_claims(repo._conn, seen_before_ms=seen_before_ms)),
        Decimal(0),
    )


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
    _start_legacy_run(clerk, envelope_clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
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
    """#2553: the provision is the entry requirement's own fee, recorded with the ENTER.

    10 shares of CAT, rounded up to the cent, carried by ``ENTER_ACCEPTED``'s
    facts, so the claim is that recorded cent and never a zero.
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
    assert _claimed(envelope_repo, seen_before_ms=T0) == Decimal("1000.01")


@pytest.mark.parametrize(
    ("broker_state", "fills", "seen_before_ms", "expected"),
    # Every unfilled remainder also claims the whole recorded one-cent fee
    # provision (owner decision 2026-09-29); no remainder claims no fee.
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
    # the claim never reads ``reserved_at_ms`` — only the
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

    assert _claimed(envelope_repo, seen_before_ms=seen_before_ms) == Decimal(expected)


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

    assert _claimed(envelope_repo, seen_before_ms=T2_OBSERVATION) == Decimal(1_000)


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


def _refuse_correction_uncertainty(reason: str) -> TransitionInput:
    raise AssertionError(f"the correction fixture must be valid: {reason}")


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
    assert _claimed(envelope_repo, seen_before_ms=T2_OBSERVATION) == Decimal("1011.25")

    # The next broker read supersedes it: once the fill counts as seen, the
    # filled order reserves nothing.
    assert _claimed(envelope_repo, seen_before_ms=T3_TRAILING_FILL + 1) == 0


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

    # 4 x 102 + the 0.50 fee, then 6 x 100 plus the whole recorded one-cent
    # provision, which the remainder claims while any of the order is unfilled.
    assert _claimed(envelope_repo, seen_before_ms=T2_OBSERVATION) == Decimal("1008.51")


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

    assert _claimed(envelope_repo, seen_before_ms=T2_OBSERVATION) == Decimal(303)


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
    assert _claimed(envelope_repo, seen_before_ms=T0) == Decimal("1000.02")


def test_a_mirror_rebuild_restores_the_recorded_reservation_and_replays_older_entries(tmp_path: Path) -> None:
    """Replay-compatible (#2553): the reservation folds back from ``ENTER_ACCEPTED``'s facts.

    An ENTER recorded without a reservation (here, one a version-1 store
    admitted with no envelope, which never reached the broker) keeps its exact
    bytes through the account's switch to budgets: the rebuilt chain has the
    same row hashes, it still reserves nothing, and the budgeted ENTER's
    recorded claim folds back unchanged.
    """
    clock = _clock_at(T0)
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    try:
        complete_fee_evidence(repo)
        _start_legacy_run(repo, clock, strategy_instance_id=ENVELOPE_SID_B, symbol="QQQ", run_id=ENVELOPE_RUN_ID_B)
        older = accept_enter(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=ENVELOPE_SID_B, decision_id="d1",
            lifecycle_run_id=ENVELOPE_RUN_ID_B, leg=_leg(symbol="QQQ", quantity=10),
        )
        _refuse_before_contact(repo, older)
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=ENVELOPE_SID_B, lifecycle_run_id=ENVELOPE_RUN_ID_B,
            clock=clock,
        )
        _register_active(repo, clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
        accept_enter(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="d1", lifecycle_run_id=RUN_ID,
            leg=_leg(quantity=10), envelope=_gate(), reference_price=100.0,
        )
        chain = [(row["sequence"], row["row_hash"]) for row in repo.custody_transitions()]
        claims = entry_cash_claims(repo._conn, seen_before_ms=T0)
        assert [(claim.strategy_instance_id, claim.unfilled_cost, claim.unfilled_fee) for claim in claims] == [
            (SID, Decimal(1_000), Decimal("0.01")),
        ]
        assert _claimed(repo, seen_before_ms=T0) == Decimal("1000.01")
        database = repo.db_path
    finally:
        repo.close()
    database.rename(database.with_suffix(".saved"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    try:
        assert [(row["sequence"], row["row_hash"]) for row in rebuilt.custody_transitions()] == chain
        assert entry_cash_claims(rebuilt._conn, seen_before_ms=T0) == claims
        assert _claimed(rebuilt, seen_before_ms=T0) == Decimal("1000.01")
    finally:
        rebuilt.close()


def _refuse_before_contact(repo: ClerkSqliteRepository, accepted: EnterSubmission) -> None:
    """The Clerk's own pre-contact refusal: the effect fails, the broker never hears of the order."""
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None
    fold_failed(
        repo, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref,
        transition_kind="ENTER_SUBMISSION_REFUSED", summary_code="MARKET_CLOSED",
        reason="The Clerk refused the entry before contacting the broker.",
        why="The market closed before the order was sent.",
    )


def _legacy_enter(repo: ClerkSqliteRepository, sid: str, run_id: str) -> EnterSubmission:
    """An ENTER a version-1 store admitted, its reservation written beside the transition (#2553).

    That earlier build priced 10 SPY at a float 100 and recorded no fee
    provision, outside the hashed facts. Nothing writes this shape any more --
    a version-1 account admits no ENTER at all -- but a store written before
    the change can still hold one.
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


def _stop(repo: ClerkSqliteRepository, sid: str, run_id: str) -> None:
    submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, lifecycle_run_id=run_id, clock=repo.clock)


def test_a_legacy_reservation_with_an_unfilled_remainder_refuses_instead_of_claiming_no_fee(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock
) -> None:
    """Regression (#2553): an unknown fee is refused under its own code, never priced at zero.

    The account is switched to budgets while the earlier entry still works at
    Alpaca. Every money read -- a Deploy's included -- refuses until that
    order ends; then its unfilled remainder is cancelled quantity, never cash.
    """
    _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
    legacy = _legacy_enter(envelope_repo, SID, RUN_ID)
    assert legacy.effect_operation_id is not None and legacy.order_ref is not None
    _stop(envelope_repo, SID, RUN_ID)
    switch_to_budgets(envelope_repo)

    with pytest.raises(EntryFeeProvisionUnrecorded) as unknown:
        envelope_repo.account_budget(cash=100_000, seen_before_ms=T0)
    assert unknown.value.reason_code == ENTRY_FEE_PROVISION_UNRECORDED
    with pytest.raises(BudgetUnavailable, match="no recorded fee estimate"):
        _register_active(envelope_repo, envelope_clock, strategy_instance_id="blocked-bot", symbol="QQQ", run_id="run-blocked")
    # Home still places the bot among those holding money.
    assert envelope_repo.bots_holding_money() == frozenset({SID})

    envelope_clock.value = T1_TERMINAL_ACK
    fold_order_evidence(
        envelope_repo,
        effect_operation_id=legacy.effect_operation_id,
        order=_observed_order(
            legacy.order_ref, status="canceled", filled_quantity=0.0, filled_avg_price=None,
            source_event_at_ms=T1_TERMINAL_ACK,
        ),
    )
    assert envelope_repo.account_budget(cash=100_000, seen_before_ms=T0).order_claims == 0
    assert envelope_repo.bots_holding_money() == frozenset()
    _register_active(envelope_repo, envelope_clock, strategy_instance_id=ENVELOPE_SID_B, symbol="QQQ", run_id=ENVELOPE_RUN_ID_B)
    assert accept_enter(
        envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=ENVELOPE_SID_B, decision_id="d1",
        lifecycle_run_id=ENVELOPE_RUN_ID_B, leg=_leg(symbol="QQQ", quantity=1),
        envelope=_gate(observed_at_ms=T1_TERMINAL_ACK), reference_price=100.0,
    ).created


def test_a_legacy_reservation_with_no_remainder_still_claims_its_unseen_fills_at_cost(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock
) -> None:
    """Only the unknown fee refuses: a fully filled legacy ENTER still reads (replay-compatible)."""
    _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
    legacy = _legacy_enter(envelope_repo, SID, RUN_ID)

    envelope_clock.value = T3_TRAILING_FILL
    _append_slice(
        envelope_repo, legacy, execution_id="legacy-fill", quantity=10.0, price=101.0, fee=1.25,
        source_event_at_ms=T3_TRAILING_FILL,
    )
    _stop(envelope_repo, SID, RUN_ID)

    assert _claimed(envelope_repo, seen_before_ms=T2_OBSERVATION) == Decimal("1011.25")


# ── An ENTER the broker never knew (#2553 review; research #2469) ─────────────


@pytest.mark.parametrize(
    "ends",
    [
        pytest.param(_refuse_before_contact, id="refused-before-contact"),
        pytest.param(
            lambda repo, accepted: fold_failed(
                repo, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref,
                summary_code="ORDER_SUBMIT_FAILED", reason="The order did not reach the broker.",
                why="potential wash trade detected. use complex orders",
            ),
            id="broker-refused-the-submit",
        ),
    ],
)
@pytest.mark.parametrize("switched", [True, False], ids=["switched-to-budgets", "still-on-version-1"])
def test_a_legacy_entry_that_never_reached_alpaca_claims_nothing(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    ends: Callable[[ClerkSqliteRepository, EnterSubmission], None],
    switched: bool,
) -> None:
    """Review major (#2553): a dead entry never blocks the account.

    Its effect failed and the broker never acknowledged it, so it can never
    fill: the unrecorded fee of its (cancelled) remainder is not an unknown.
    On a switched account the next bot is deployed and enters; on one still on
    version 1 the next ENTER is refused for that reason alone.
    """
    _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=SID, symbol="SPY", run_id=RUN_ID)
    legacy = _legacy_enter(envelope_repo, SID, RUN_ID)
    ends(envelope_repo, legacy)
    _stop(envelope_repo, SID, RUN_ID)

    [claim] = entry_cash_claims(envelope_repo._conn, seen_before_ms=T0)
    assert (claim.unfilled_cost, claim.unseen_fill_cost, claim.unfilled_fee) == (0, 0, 0)
    assert envelope_repo.bots_holding_money() == frozenset()
    if switched:
        _register_active(envelope_repo, envelope_clock, strategy_instance_id=ENVELOPE_SID_B, symbol="QQQ", run_id=ENVELOPE_RUN_ID_B)
        assert envelope_repo.account_budget(cash=100_000, seen_before_ms=T0).order_claims == 0
        assert accept_enter(
            envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=ENVELOPE_SID_B, decision_id="d1",
            lifecycle_run_id=ENVELOPE_RUN_ID_B, leg=_leg(symbol="QQQ", quantity=1), envelope=_gate(), reference_price=100.0,
        ).created
    else:
        _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=ENVELOPE_SID_B, symbol="QQQ", run_id=ENVELOPE_RUN_ID_B)
        with pytest.raises(AdmissionBlockedError) as exc_info:
            accept_enter(
                envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=ENVELOPE_SID_B, decision_id="d1",
                lifecycle_run_id=ENVELOPE_RUN_ID_B, leg=_leg(symbol="QQQ", quantity=1), envelope=_gate(),
                reference_price=100.0,
            )
        assert exc_info.value.decision.reason_code == BUDGETS_NOT_SWITCHED_ON


@pytest.mark.parametrize("refusal", ["broker-refused-the-submit", "refused-before-contact"])
async def test_a_budgeted_enter_that_never_reached_the_book_releases_its_claim_and_the_stopped_bot_is_finished(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str], refusal: str
) -> None:
    """Research #2469's leak: a refused entry's cash stayed claimed after Stop, and the bot never showed Finished."""
    sid, run_id = active_instance
    leg = _leg(quantity=10)
    accepted = accept_enter(
        envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id="d1", lifecycle_run_id=run_id,
        leg=leg, envelope=_gate(), reference_price=100.0,
    )
    if refusal == "broker-refused-the-submit":
        await submit_accepted_enter(
            envelope_repo, accepted=accepted, leg=leg,
            trade=_FakeTradePort(submit_error=BrokerOrderRejected("potential wash trade detected. use complex orders")),
        )
    else:
        await submit_accepted_enter(
            envelope_repo, accepted=accepted, leg=leg, trade=_FakeTradePort(),
            before_submit=lambda: EntrySubmissionRefusal(
                summary_code="MARKET_CLOSED", why="The market closed before the order was sent.",
            ),
        )
    effect = envelope_repo.effect_operation(accepted.effect_operation_id or "")
    assert effect is not None and effect.state == "failed"
    _stop(envelope_repo, sid, run_id)

    budget = envelope_repo.account_budget(cash=100_000, seen_before_ms=T0)

    assert budget.order_claims == 0
    assert budget.deployments[0].pending_orders == 0
    assert sid not in envelope_repo.bots_holding_money()


async def test_an_enter_whose_submission_is_unknown_keeps_its_whole_claim_until_it_is_resolved(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock, active_instance: tuple[str, str]
) -> None:
    """An unknown outcome is not a dead order: the claim stands until absence is proven.

    The submit's answer is lost, so the effect is ``unknown`` and the order
    may yet be working at the broker. Only once the absence grace has passed
    and the exact lookup still finds nothing is the entry dead.
    """
    sid, run_id = active_instance
    leg = _leg(quantity=10)
    accepted = accept_enter(
        envelope_repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id="d1", lifecycle_run_id=run_id,
        leg=leg, envelope=_gate(), reference_price=100.0,
    )
    assert accepted.order_ref is not None
    absent = _FakeTradePort(submit_error=BrokerUnavailable("The submit's answer was lost."), lookup_absent=True)
    await submit_accepted_enter(envelope_repo, accepted=accepted, leg=leg, trade=absent)

    effect = envelope_repo.effect_operation(accepted.effect_operation_id or "")
    assert effect is not None and effect.state == "unknown"
    assert _claimed(envelope_repo, seen_before_ms=T0) == Decimal("1000.01")
    assert envelope_repo.bots_holding_money() == frozenset({sid})

    envelope_clock.advance(submit_absence_grace_ms())
    await resolve_enter_submission(envelope_repo, order_ref=accepted.order_ref, trade=absent)

    effect = envelope_repo.effect_operation(accepted.effect_operation_id or "")
    assert effect is not None and effect.state == "failed"
    assert _claimed(envelope_repo, seen_before_ms=T0) == 0
