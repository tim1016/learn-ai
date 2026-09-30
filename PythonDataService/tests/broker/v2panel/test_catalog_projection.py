"""SQLite catalog projection coverage."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import pytest

from app.broker.alpaca.clerk.sqlite.budget_projection import BotResult
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ClerkScope,
    ProjectedHold,
    ProjectionGuidance,
    RecoveryCapability,
    RecoveryConfirmation,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryActionId
from app.schemas.broker_bots import BotStatusView
from app.schemas.live_runs import BotDutyOutcomeView
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
    status_label_for,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import build_sqlite_catalog, with_finished_results
from tests.broker.v2panel.fixtures import ACCT, OTHER_SID, SID, home_facts, read_results_from


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


def _economic_snapshot(
    *, sid: str, control_revision: int = 42, exposure: dict[str, float] | None = None,
) -> EconomicSnapshot:
    return EconomicSnapshot(
        account_id=ACCT,
        strategy_instance_id=sid,
        authority_generation=7,
        control_revision=control_revision,
        session_open_ms=1_700_000_000_000,
        session_close_ms=1_700_023_400_000,
        recent_fills=(),
        fills_today=3,
        exposure={"SPY": 2.0} if exposure is None else exposure,
        realized_pnl_today=12.5,
        exact_open_pnl=Decimal("3.25"),
        marks_complete=True,
        mark_observed_at_ms={"SPY": 1_700_010_000_000},
        fee_fidelity="reported",
        execution_coverage="complete",
        last_activity_at_ms=1_700_010_000_000,
    )


def _hold(
    *,
    scope: ClerkScope,
    sid: str | None,
    hold_id: str = "hold-1",
    reason_code: str = "EXPOSURE_UNRECONCILED",
) -> ProjectedHold:
    """One ACTIVE hold at its real scope.

    ``scope`` is the field the row-command behaviour turns on, so the fake has
    to be the real frozen dataclass: a bare string cannot distinguish an
    account-wide problem from a bot-scoped one.
    """
    return ProjectedHold(
        hold_id=hold_id,
        scope=scope,
        strategy_instance_id=sid,
        reason_code=reason_code,
        opened_at_ms=1_700_000_000_000,
        evidence_refs=(),
    )


def _projection(
    *,
    sid: str,
    control_revision: int = 42,
    holds: tuple[ProjectedHold, ...] = (),
    recovery_actions: tuple[RecoveryCapability, ...] = (),
) -> ClerkProjection:
    return ClerkProjection(
        account_id=ACCT,
        strategy_instance_id=sid,
        authority_generation=7,
        db_identity_token="db-7",
        authority_health="healthy",
        authority_health_reason=None,
        control_revision=control_revision,
        custody_owner="ACCOUNT_CLERK",
        runs=(),
        commands=(),
        operations=(),
        working_order_refs=(),
        positions=(),
        holds=holds,
        uncertainties=(),
        latest_reconciliation=None,
        terminal_receipts=(),
        guidance=ProjectionGuidance(
            headline="Account Clerk custody is healthy",
            explanation="SQLite has current custody truth.",
            scope="CUSTODY_SUBJECT",
            impact="Normal Clerk-governed controls remain available.",
            custody_owner="ACCOUNT_CLERK",
            may_create_exposure=True,
            available_safety_actions=(),
            action_required=False,
            next_step="No recovery action is required.",
        ),
        recovery_actions=recovery_actions,
        generated_at_ms=1_700_010_000_000,
    )


def _recovery_capability(
    *,
    primary: bool = True,
    available: bool = True,
    action_id: RecoveryActionId = "cancel_verified_working_orders",
    label: str = "Cancel verified working orders",
) -> RecoveryCapability:
    return RecoveryCapability(
        action_id=action_id,
        label=label,
        explanation="Cancel the orders the Clerk can still prove it owns.",
        available=available,
        unavailable_reason_code=None if available else "EVIDENCE_UNAVAILABLE",
        unavailable_reason=None if available else "Broker truth is unavailable.",
        scope="CUSTODY_SUBJECT",
        freshness="fresh",
        evidence=(),
        reduction_plan=None,
        confirmation=RecoveryConfirmation(
            title="Cancel working orders?",
            explanation="The runtime stops before new orders are submitted.",
            confirm_label="Cancel orders",
        ),
        next_step="Cancel the orders, then reconcile.",
        concurrency_token="recovery-token",
        execution_ref=None,
        mutation=True,
        primary=primary,
    )


def test_status_label_maps_the_closed_vocabulary() -> None:
    assert status_label_for(_status(sid=SID, running=True)) == "Working"
    assert status_label_for(_status(sid=SID, running=False, phase="OFF_DUTY")) == "Off duty"
    assert status_label_for(_status(sid=SID, phase="RETIRED", running=False)) == "Retired"


def test_an_unclean_exit_is_labelled_distinctly_from_a_deliberate_stop() -> None:
    """S3b: three bots died mid-run and the roster read "Off duty . Flat".

    The audit and `known-gaps.md` both record this as `needs_attention=false`.
    That is wrong -- attention was already true for a crash, and the backend
    already authored crash-specific `status_explanation`. What actually hid
    the failure is the label: the roster renders `status_label`, and a crash
    mapped to the same "Off duty" a clean stop produces.

    The labels come from the shared operator-copy vocabulary rather than new
    strings invented here.
    """
    crashed = _status(sid=SID, running=False, phase="OFF_DUTY", duty_kind="CRASHED")
    unverified = _status(
        sid=SID, running=False, phase="OFF_DUTY", duty_kind="EXITED_UNVERIFIED"
    )
    stopped = _status(sid=SID, running=False, phase="OFF_DUTY", duty_kind="STOPPED")

    assert status_label_for(crashed) == "Crashed"
    assert status_label_for(unverified) == "Exited unverified"
    # A clean stop is still plain "Off duty" -- this must not become alarming.
    assert status_label_for(stopped) == "Off duty"
    # A retired bot keeps its terminal label whatever ended the last run.
    assert (
        status_label_for(
            _status(sid=SID, phase="RETIRED", running=False, duty_kind="CRASHED")
        )
        == "Retired"
    )


def test_sqlite_catalog_uses_config_identity_and_one_economic_rollup() -> None:
    status = _status(
        sid=SID, strategy_key="opening_range_breakout", mode="trade"
    ).model_copy(update={"strategy_label": "Opening Range Breakout — Paper"})
    catalog = build_sqlite_catalog(
        [status],
        projections={SID: _projection(sid=SID)},
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
        home=home_facts(),
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
            home=home_facts(),
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
            home=home_facts(),
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
            home=home_facts(),
        )


def test_sqlite_catalog_refuses_custody_and_economics_from_different_revisions() -> None:
    with pytest.raises(SqliteCatalogRevisionMismatch, match="do not share one authority revision"):
        build_sqlite_catalog(
            [_status(sid=SID, strategy_key="opening_range_breakout", mode="trade")],
            projections={SID: _projection(sid=SID, control_revision=41)},
            economic_rollups={SID: _economic_snapshot(sid=SID, control_revision=42)},
            account_id=ACCT,
            home=home_facts(),
        )


# ── S2/S4 (#1778): the roster's per-row recovery command ─────────────────────
# `BotCatalogView.row_action` existed on the wire with no producer -- the
# SQLite adaptation hardcoded `None` -- so an attention row offered the
# operator no command at all. The row now carries its primary recovery
# capability, built by the same `_panel_action` the panel uses so the
# revision/token guard *and* the typed confirmation travel with it.


def test_an_attention_row_carries_its_primary_recovery_command() -> None:
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(),),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
        home=home_facts(),
    )

    row = catalog[0]
    assert row.needs_attention is True
    assert row.row_action is not None
    assert row.row_action.action_id == "cancel_verified_working_orders"
    # The guard contract is the panel's, not a roster-local invention.
    assert row.row_action.revision == 42
    assert row.row_action.concurrency_token == "recovery-token"
    # Promoting the command must not drop its typed confirmation.
    assert row.row_action.confirmation is not None
    assert row.row_action.confirmation.confirm_label == "Cancel orders"


def test_an_unavailable_recovery_command_is_offered_with_its_blocker() -> None:
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(available=False),),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
        home=home_facts(),
    )

    row_action = catalog[0].row_action
    assert row_action is not None
    # Honest rather than hidden: the row shows why it cannot act.
    assert row_action.enabled is False
    assert row_action.blockers


def test_a_healthy_row_carries_no_recovery_command() -> None:
    """Contract regression the other way.

    A row with nothing wrong must stay a plain roster row. Recovery commands
    are for rows that need attention; offering one everywhere would make the
    rail alarming and the command meaningless.
    """
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={SID: _projection(sid=SID, recovery_actions=(_recovery_capability(),))},
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
        home=home_facts(),
    )

    assert catalog[0].needs_attention is False
    assert catalog[0].row_action is None


def test_an_attention_row_without_a_primary_capability_offers_nothing() -> None:
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(primary=False),),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
        home=home_facts(),
    )

    assert catalog[0].row_action is None


def test_a_crash_with_no_custody_problem_offers_no_reconcile_on_the_rail() -> None:
    """``reconcile_now`` is a refresh, and the rail asks for a cure.

    ``recovery_policy`` marks ``reconcile_now`` ``available=True``
    unconditionally -- it reads no hold, uncertainty or exposure -- so it wins
    ``_primary_action_id`` for any attention row whose genuinely-gated cures
    are all unavailable. This bot's attention comes from an unclean exit, not
    from custody: ``holds`` and ``uncertainties`` are both empty, so there is
    nothing to reconcile. It showed "Reconcile now" on the rail anyway, while
    its own panel read "No recovery action is required".

    The row still says it needs attention, because it does. What it must not do
    is name a refresh as the cure. The panel keeps offering it.
    """
    catalog = build_sqlite_catalog(
        [_status(sid=SID, running=False, phase="OFF_DUTY", duty_kind="CRASHED")],
        projections={
            SID: _projection(
                sid=SID,
                recovery_actions=(
                    _recovery_capability(action_id="reconcile_now", label="Reconcile now"),
                ),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
        home=home_facts(),
    )

    row = catalog[0]
    assert row.needs_attention is True
    assert row.row_action is None


def test_a_bot_scoped_custody_problem_keeps_its_reconcile_command() -> None:
    """The other half of the rule, and the one that is easy to break.

    Suppressing ``reconcile_now`` by action id alone would strip the command
    from rows where reconciliation *is* the authored cure: an
    ``ORDER_OUTCOME_UNKNOWN`` uncertainty names it as its own next step, and a
    stranded position without a clean account reconciliation leaves every
    higher-priority action unavailable, so ``reconcile_now`` is legitimately
    primary and legitimately the only command. Dropping it there is exactly the
    "attention row with no command at all" #1778 exists to prevent.
    """
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(
                    _recovery_capability(action_id="reconcile_now", label="Reconcile now"),
                ),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
        home=home_facts(),
    )

    row_action = catalog[0].row_action
    assert row_action is not None
    assert row_action.action_id == "reconcile_now"


def test_an_account_scoped_hold_puts_no_per_bot_command_on_any_row() -> None:
    """An account-scoped problem has an account-scoped cure.

    ``ClerkSqliteProjectionReader._holds`` folds every ``ACCOUNT_CLERK`` row
    into *each* bot's snapshot, so one account-wide hold marks the whole fleet
    ``needs_attention``. Deriving the row command from that fold would hand N
    operators N per-bot mutation buttons for one problem -- the same fan-out
    defect family as the account-wide entry freeze this PRD removes.

    Attention itself stays: the rows are genuinely affected, and saying so is
    honest. What must not appear is the button.
    """
    account_hold = _hold(scope="ACCOUNT_CLERK", sid=None, hold_id="hold-account")
    catalog = build_sqlite_catalog(
        [_status(sid=SID), _status(sid=OTHER_SID, strategy_key="ema_crossover_signal")],
        projections={
            SID: _projection(
                sid=SID,
                holds=(account_hold,),
                recovery_actions=(_recovery_capability(),),
            ),
            OTHER_SID: _projection(
                sid=OTHER_SID,
                holds=(account_hold,),
                recovery_actions=(_recovery_capability(),),
            ),
        },
        economic_rollups={
            SID: _economic_snapshot(sid=SID),
            OTHER_SID: _economic_snapshot(sid=OTHER_SID),
        },
        account_id=ACCT,
        home=home_facts(),
    )

    assert len(catalog) == 2
    assert [row.needs_attention for row in catalog] == [True, True]
    assert [row.row_action for row in catalog] == [None, None]


def test_a_bot_scoped_hold_still_commands_only_its_own_row() -> None:
    """The other half of the scope contract.

    Narrowing the derivation to ``CUSTODY_SUBJECT`` must not mute the case it
    exists for: the bot that actually holds the stranded exposure keeps its
    command, and its unaffected sibling gets none.
    """
    catalog = build_sqlite_catalog(
        [_status(sid=SID), _status(sid=OTHER_SID, strategy_key="ema_crossover_signal")],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(),),
            ),
            OTHER_SID: _projection(
                sid=OTHER_SID,
                recovery_actions=(_recovery_capability(),),
            ),
        },
        economic_rollups={
            SID: _economic_snapshot(sid=SID),
            OTHER_SID: _economic_snapshot(sid=OTHER_SID),
        },
        account_id=ACCT,
        home=home_facts(),
    )

    held, sibling = catalog
    assert held.row_action is not None
    assert held.row_action.action_id == "cancel_verified_working_orders"
    assert sibling.needs_attention is False
    assert sibling.row_action is None


# ── Home groups (PRD #2560 D5/D7) ────────────────────────────────────────────


async def _home_row(
    *,
    running: bool,
    exposure: dict[str, float],
    holding: tuple[str, ...] = (),
    world: Literal["real_paper", "real_live", "shadow", "synthetic"] = "real_paper",
    mode: Literal["log_only", "dry_run", "trade"] = "trade",
    results: dict[str, BotResult] | None = None,
    latest_stop_ms: int | None = 7,
):
    rows = build_sqlite_catalog(
        [_status(sid=SID, mode=mode, running=running, phase="ON_DUTY" if running else "OFF_DUTY",
                 desired_state="RUNNING" if running else "STOPPED", duty_kind=None if running else "STOPPED")],
        projections={SID: _projection(sid=SID)},
        economic_rollups={SID: _economic_snapshot(sid=SID, exposure=exposure)},
        account_id=ACCT,
        home=home_facts(world=world, holding=holding, latest_stops={SID: latest_stop_ms}),
    )
    [row] = await with_finished_results(rows, read_results_from(results))
    return row


async def test_a_running_bot_is_running_whatever_it_holds() -> None:
    row = await _home_row(running=True, exposure={"SPY": 2.0}, holding=(SID,))

    assert (row.group, row.world_label, row.ended_at_ms) == ("running", "PAPER · practice money", None)
    assert row.status_explanation == "Running · holds 2 SPY"
    assert row.final_result_usd is None


async def test_a_stopped_bot_with_shares_is_holding_and_says_no_bot_manages_them() -> None:
    row = await _home_row(running=False, exposure={"SPY": 2.0}, holding=(SID,))

    assert row.group == "holding"
    assert row.status_explanation == "Stopped · still holds 2 SPY · no bot is managing it"
    # Its run's own stop instant, never its status's last transition.
    assert row.ended_at_ms == 7


async def test_a_run_that_ended_without_a_stop_instant_ended_at_its_duty_outcome() -> None:
    """A crash leaves its run row open: the outcome's record time is the end."""
    row = await _home_row(running=False, exposure={}, latest_stop_ms=None)

    assert row.ended_at_ms == 1


async def test_a_stopped_flat_bot_whose_entry_still_claims_money_is_holding_not_finished() -> None:
    row = await _home_row(running=False, exposure={}, holding=(SID,))

    assert row.group == "holding"
    assert row.status_explanation == "Stopped · an entry order is still working · no bot is managing it"
    assert row.final_result_usd is None


async def test_a_stopped_flat_fully_released_bot_is_finished_with_its_whole_life_result() -> None:
    row = await _home_row(running=False, exposure={}, results={SID: BotResult(result=Decimal("9.9750"), trade_count=2)})

    assert row.group == "finished"
    # Python authors the dollars: half-even display cents, sign kept.
    assert (row.final_result_usd, row.trade_count, row.ended_at_ms) == ("9.98", 2, 7)
    assert row.status_explanation == "Off duty and flat."


async def test_a_finished_loss_keeps_its_sign() -> None:
    row = await _home_row(running=False, exposure={}, results={SID: BotResult(result=Decimal("-0.22"), trade_count=3)})

    assert row.final_result_usd == "-0.22"


async def test_a_finished_result_the_fees_cannot_vouch_for_is_unknown_never_zero() -> None:
    row = await _home_row(running=False, exposure={})

    assert row.group == "finished"
    assert (row.final_result_usd, row.trade_count) == (None, None)


@pytest.mark.parametrize(
    ("world", "mode", "running"),
    [
        ("synthetic", "trade", True),
        ("synthetic", "trade", False),
        # H23 / review B6: a Dry Run under a real account is worded as a Dry
        # Run, never with its account's real-money label.
        ("real_paper", "dry_run", False),
        ("real_live", "dry_run", True),
    ],
)
async def test_a_dry_run_is_its_own_group_while_it_runs_or_holds(world, mode, running) -> None:
    row = await _home_row(running=running, exposure={"SPY": 1.0}, holding=(SID,), world=world, mode=mode)

    assert (row.group, row.world_label) == ("dry_run", "DRY RUN · simulated cash")
    assert row.final_result_usd is None


@pytest.mark.parametrize(("world", "mode"), [("synthetic", "trade"), ("real_paper", "dry_run")])
async def test_a_stopped_flat_dry_run_is_finished_under_its_own_world_label(world, mode) -> None:
    """Owner decision 2026-09-28 (#2567): the Finished fold is the one place
    a bot is cleared from, Dry Runs included. A stopped, flat Dry Run is
    Finished like any other bot, still worded as a Dry Run, never as the
    account's money (D5)."""
    row = await _home_row(
        running=False, exposure={}, world=world, mode=mode,
        results={SID: BotResult(result=Decimal("1.25"), trade_count=2)},
    )

    assert (row.group, row.world_label) == ("finished", "DRY RUN · simulated cash")
    assert (row.final_result_usd, row.trade_count) == ("1.25", 2)


@pytest.mark.parametrize(
    ("world", "label"),
    [("real_live", "LIVE · real money"), ("shadow", "SHADOW · simulated fills on your live account")],
)
async def test_every_world_is_worded_one_way(world, label) -> None:
    assert (await _home_row(running=True, exposure={}, world=world)).world_label == label


async def test_finished_results_are_read_off_the_event_loop() -> None:
    """Review A4: the lifetime fee projection is blocking work; a Home poll
    must never run it on the event loop thread."""
    import threading

    loop_thread = threading.current_thread()
    readers: list[threading.Thread] = []

    def read(sids):
        readers.append(threading.current_thread())
        return {sid: BotResult(result=Decimal("1"), trade_count=1) for sid in sids}

    rows = build_sqlite_catalog(
        [_status(sid=SID, mode="trade", running=False, phase="OFF_DUTY", desired_state="STOPPED", duty_kind="STOPPED")],
        projections={SID: _projection(sid=SID)},
        economic_rollups={SID: _economic_snapshot(sid=SID, exposure={})},
        account_id=ACCT,
        home=home_facts(),
    )
    [row] = await with_finished_results(rows, read)

    assert row.final_result_usd == "1.00"
    assert readers and readers[0] is not loop_thread
