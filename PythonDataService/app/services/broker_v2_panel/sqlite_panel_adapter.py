"""Adapt SQLite Clerk folds to the existing Broker V2 panel contract.

The panel remains the product surface.  This module is only a projection
adapter: it neither replays transitions nor authors a second recovery policy.
Every recovery action and its token comes from the SQLite recovery catalog;
Clear's ``archive`` comes from the one archive rule (``action_policy``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.account_authority import authority_kind_for_account
from app.broker.alpaca.clerk.account_money import holdings_text
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import display_dollars
from app.broker.alpaca.clerk.program_leg import LegRefusal
from app.broker.alpaca.clerk.recovery_reduction import (
    realized_slippage_bps,
    realized_slippage_cost,
)
from app.broker.alpaca.clerk.sqlite.budget_projection import BotResult
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.exit_resolution import priced_reduction_reference_price
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ProjectedOperation,
    ProjectedOrder,
    RecoveryCapability,
)
from app.broker.alpaca.clerk.sqlite.reads import NONTERMINAL_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.recovery_policy import FRESH_EVIDENCE_MAX_AGE_MS, UNCONDITIONAL_RECOVERY_ACTION_IDS
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.v2panel.action_policy import archive_action
from app.broker.v2panel.vocabulary import copy_for
from app.schemas.account_authority import SIMULATED_AUTHORITY_KINDS, AuthorityKind
from app.schemas.bot_lifecycle import UNCLEAN_DUTY_OUTCOMES
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotHealthCard,
    BotPanelView,
    ExposureNoticeView,
    MissionVerdictView,
    PanelAction,
    ReadinessCheckView,
    RecentFillView,
    StationView,
    TransactionRail,
    WorkingOrderView,
)
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    ConfirmInFormAction,
    OperatorBlocker,
    OperatorConfirmationCopy,
    OperatorMove,
)
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
    compose_catalog_view,
    require_sqlite_catalog_identity,
    sqlite_catalog_rollup,
)
from app.services.broker_v2_panel.panel_projection_service import open_pnl_fields, select_primary_action
from app.services.session_authority import SessionAuthorityState

logger = logging.getLogger(__name__)

_WORKING_BROKER_STATES = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "pending_cancel"}
)
# Matches the established station-derivation semantic (`station_derivation.py`
# `_fill_station`): FILL is satisfied only by actual fill evidence, never by
# any terminal outcome — a canceled/expired/rejected order definitively
# received zero fill and must not render as satisfied.
_FILLED_BROKER_STATES = frozenset({"filled", "partially_filled"})

# SQLite owns broker-recovery actions after activation, but the bot-lifecycle
# action (Clear's ``archive``) remains the runner's: this adapter presents it
# (``_lifecycle_actions``) and membership here routes its POST past the SQLite
# recovery executor to the runner's performer (`sqlite_panel_source`).
SQLITE_PANEL_LIFECYCLE_ACTION_IDS = frozenset({"archive"})


def adapt_sqlite_panel(
    panel: BotPanelView,
    projection: ClerkProjection,
    *,
    account_mode: Literal["paper", "live"],
    economics: EconomicSnapshot | None = None,
    repository: ClerkSqliteRepository | None = None,
    flatten_verdict: SessionAuthorityState | LegRefusal | None = None,
) -> BotPanelView:
    """Replace JSONL-derived custody fields with one SQLite fold snapshot.

    ``account_mode`` is the mode the projection's authority learned from the
    broker (the facade's ``account_mode``); it labels each fill ``real_paper``
    or ``real_live`` and has no default (#2823).

    Active callers supply ``economics`` at the identical SQLite revision as
    ``projection``.  The optional form remains only for narrow historical
    adapter tests; the active source fails closed before calling this adapter
    without authoritative economic evidence.

    ``repository`` is optional and used only as the stored-key fallback when
    the selected transaction ref is absent from the bounded
    ``projection.operations`` window (its 50/100-row cap).  Without it, a ref
    outside the window renders as "not found" rather than being resolved.

    ``flatten_verdict`` is the session an operator's flatten sent now would go
    out in, or the refusal the Clerk gives it
    (``SqliteAlpacaClerkFacade.flatten_send_verdict``). Outside the regular
    session the generic Execute safe flatten button is not the way to flatten
    (#2007): see ``_flatten_session_blocker``.

    """
    if economics is not None:
        _require_coherent_economic_snapshot(projection, economics)
    actions = [
        *_lifecycle_actions(panel, projection),
        *(
            _panel_action(item, projection.control_revision, flatten_verdict=flatten_verdict)
            for item in projection.recovery_actions
        ),
    ]
    checks = [_readiness_check(item, projection.generated_at_ms) for item in projection.recovery_actions]
    ready_count = sum(check.ready for check in checks)
    recovery_cure = _recovery_cure(projection, bot_owns_problem=has_bot_scoped_custody_problem(projection))
    exposure = {
        position.symbol: position.attributed_qty
        for position in projection.positions
        if position_quantity_is_nonzero(position.attributed_qty)
    }
    return panel.model_copy(
        update={
            "health": _with_terminal_exposure_notices(panel, projection),
            "updated_at_ms": projection.generated_at_ms,
            "revision": projection.control_revision,
            "mission_verdict": _mission_verdict(panel, projection),
            "rail": _transaction_rail(
                projection,
                selected_ref=panel.rail.transaction_ref,
                repository=repository,
            ),
            "journal_tail_ref": (
                f"/api/alpaca-clerk-sqlite/accounts/{projection.account_id}/bots/"
                f"{projection.strategy_instance_id}/timeline"
            ),
            "journal_tail_seq": projection.control_revision,
            "actions": actions,
            "primary_action": select_primary_action(
                actions,
                panel.health,
                holds_position=bool(exposure),
                recovery_primary_action_id=None if recovery_cure is None else recovery_cure.action_id,
            ),
            "exit_terms": None if repository is None else repository.exit_terms(projection.strategy_instance_id),
            "readiness_checks": checks,
            "readiness_ready_count": ready_count,
            "readiness_blocked_count": len(checks) - ready_count,
            "exposure": exposure,
            "working_orders": _working_orders(
                panel,
                projection,
                strict=economics is not None,
            ),
            # SQLite is the sole custody projection after activation.  Legacy
            # JSONL fill/P&L rollups must not be mixed into this evidence cut.
            "recent_fills": (
                []
                if economics is None
                else [
                    recent_fill_view(
                        fill,
                        authority_account_id=projection.account_id,
                        account_mode=account_mode,
                        repository=repository,
                    )
                    for fill in economics.recent_fills
                ]
            ),
            "fills_today": None if economics is None else economics.fills_today,
            "realized_pnl_today": (
                None if economics is None else economics.realized_pnl_today
            ),
            **open_pnl_fields(None if economics is None else economics.exact_open_pnl),
        }
    )


def _lifecycle_actions(panel: BotPanelView, projection: ClerkProjection) -> list[PanelAction]:
    """Clear's ``archive`` for a stopped bot's page; a running bot's page has none.

    A running bot's stop is the recovery catalog's ``stop_bot_decisions``.
    ``panel`` is the pre-adaptation projection: archive reads its runner
    liveness and phase, the Clerk card's freeze and the SQLite exposure it was
    built from. The working-order count is the Clerk's own
    (``projection.working_order_refs``), the set the commit-time check reads.
    The pre-adaptation projection's list comes from the legacy order journal,
    which nothing writes any more, so a working order never disabled the
    button (#2635).
    """
    if panel.health.running:
        return []
    return [
        archive_action(
            running=panel.health.running,
            phase=panel.health.phase,
            freeze_active=panel.clerk.freeze_active,
            exposure=panel.exposure,
            working_order_count=len(projection.working_order_refs),
            account_id=panel.account_id,
            strategy_instance_id=panel.strategy_instance_id,
            revision=projection.control_revision,
        )
    ]


def has_bot_scoped_custody_problem(projection: ClerkProjection) -> bool:
    """True when THIS bot -- not the account -- owns a custody problem.

    ``ClerkSqliteProjectionReader._holds`` and ``._uncertainties`` deliberately
    fold every ``ACCOUNT_CLERK`` row into each bot's snapshot, because an
    account-wide hold really does constrain every bot. That fold is right for
    telling an operator the row is affected; it is wrong for deciding whose
    button cures it.
    """
    return any(
        item.scope == "CUSTODY_SUBJECT"
        for item in (*projection.holds, *projection.uncertainties)
    )


def _catalog_row_action(
    projection: ClerkProjection,
    *,
    row_needs_attention: bool,
) -> PanelAction | None:
    """The one recovery command a roster row may dispatch, or ``None``.

    Only a row whose *own* trouble is bot-scoped carries one: either the
    roster row already flagged itself (an unclean terminal exit, this bot's
    incomplete execution coverage) or the Clerk holds a ``CUSTODY_SUBJECT``
    hold/uncertainty against it. A healthy row's routine lifecycle commands
    belong to the panel, and a compact rail has no room to justify a mutation
    nobody asked for.

    Account-scoped trouble is excluded on purpose. An ``ACCOUNT_CLERK`` hold
    (and likewise degraded authority health, which is a property of the
    account's SQLite authority, not of any bot) reaches every row through the
    fold above; deriving the command from it would print N identical per-bot
    mutation buttons for one account-scoped problem. An account-scoped problem
    has an account-scoped cure, and fanning it out per bot is the same defect
    family as an account-wide entry freeze. Such a row still reads
    ``needs_attention`` -- it is genuinely affected -- it just offers no
    button.

    This previously returned ``None`` unconditionally, on the reasoning that
    "recovery mutations require the bot panel's typed confirmation flow". The
    premise was right and the conclusion was wrong: ``_panel_action`` is the
    same builder the panel uses, so the capability's revision, concurrency
    token, blockers **and typed confirmation** all travel with it. Emitting it
    here carries that flow onto the row rather than bypassing it -- while
    returning ``None`` left an attention row with no command at all (#1778).

    An ``UNCONDITIONAL_RECOVERY_ACTION_IDS`` primary needs a custody problem to
    reach the rail, and that test lives only here. Those capabilities are
    available without reading any custody state, so they win
    ``_primary_action_id`` for any attention row whose genuinely-gated cures are
    all unavailable -- printing "Reconcile now" next to a bot that crashed flat
    with empty ``holds`` and ``uncertainties``, while that same bot's panel read
    "No recovery action is required". Being always available, such an action
    proves nothing by *being* primary; the rail asks "what is this row's cure?"
    and it cannot answer on its own.

    It is emphatically not suppressed outright. When this bot owns a hold or
    uncertainty, reconciliation is frequently the authored cure -- an
    ``ORDER_OUTCOME_UNKNOWN`` uncertainty names it as its own next step, and a
    stranded position without a clean account reconciliation leaves every
    earlier action unavailable. Dropping the button there would strip the row's
    only command and re-open #1778. So the custody problem, not the action id,
    is what decides: the same predicate that admits a non-attention row, asked
    a second time of an action that cannot speak for itself.

    The panel is untouched either way -- a voluntary custody refresh is a
    legitimate operator move, just not a row-level alarm. Which actions have
    this property is the recovery policy's fact, imported rather than restated,
    so renaming one cannot leave a stale literal here silently re-surfacing the
    button.
    """
    bot_owns_problem = has_bot_scoped_custody_problem(projection)
    if not row_needs_attention and not bot_owns_problem:
        return None
    cure = _recovery_cure(projection, bot_owns_problem=bot_owns_problem)
    return None if cure is None else _panel_action(cure, projection.control_revision)


def _recovery_cure(projection: ClerkProjection, *, bot_owns_problem: bool) -> RecoveryCapability | None:
    """This bot's recovery cure -- the policy's primary capability -- or ``None``.

    The one answer to "what is this bot's cure?", shared by the roster row and
    the bot page's primary command. An ``UNCONDITIONAL_RECOVERY_ACTION_IDS``
    primary is available without reading any custody state, so being primary
    proves nothing on its own: it is this bot's cure only while the bot owns a
    custody problem (see ``_catalog_row_action``). Otherwise it stays an
    ordinary command on the page, never its primary one.
    """
    primary = next((item for item in projection.recovery_actions if item.primary), None)
    if primary is None:
        return None
    if primary.action_id in UNCONDITIONAL_RECOVERY_ACTION_IDS and not bot_owns_problem:
        return None
    return primary


def adapt_sqlite_catalog(
    rows: list[BotCatalogView],
    projections: dict[str, ClerkProjection],
    economic_rollups: dict[str, EconomicSnapshot],
) -> list[BotCatalogView]:
    """Replace roster economics with the one S2 SQLite rollup revision."""
    adapted: list[BotCatalogView] = []
    for row in rows:
        economics = economic_rollups.get(row.strategy_instance_id)
        if economics is None:
            raise SqliteCatalogProjectionUnavailable(
                f"Bot '{row.strategy_instance_id}' has no SQLite economic snapshot."
            )
        projection = projections.get(row.strategy_instance_id)
        exposure = {
            symbol: quantity
            for symbol, quantity in economics.exposure.items()
            if position_quantity_is_nonzero(quantity)
        }
        economic_updates: dict[str, object] = {
            "exposure": exposure,
            "fills_today": economics.fills_today,
            "realized_pnl_today": economics.realized_pnl_today,
            "open_pnl": economics.open_pnl,
            "last_activity_at_ms": economics.last_activity_at_ms,
        }
        if projection is None:
            adapted.append(row.model_copy(update=economic_updates))
            continue
        needs_attention = row.needs_attention or _clerk_needs_attention(projection)
        adapted.append(
            row.model_copy(
                update={
                    **economic_updates,
                    "needs_attention": needs_attention,
                    "status_explanation": _sqlite_catalog_explanation(
                        row,
                        projection,
                        exposure=exposure,
                    ),
                    "row_action": _catalog_row_action(
                        projection, row_needs_attention=row.needs_attention
                    ),
                }
            )
        )
    return adapted


@dataclass(frozen=True)
class CatalogHomeFacts:
    """What places an authority's rows on its account's Home (PRD #2560).

    ``world`` is the authority's own; ``holding_money`` the bots with position
    cost or still-claimed money; ``latest_stops`` each bot's newest run's stop
    instant (``ClerkSqliteRepository.latest_run_stops``).
    """

    world: AuthorityKind
    holding_money: frozenset[str]
    latest_stops: Mapping[str, int | None]


def build_sqlite_catalog(
    statuses: list[BotStatusView],
    projections: dict[str, ClerkProjection],
    *,
    economic_rollups: dict[str, EconomicSnapshot],
    account_id: str,
    home: CatalogHomeFacts,
) -> list[BotCatalogView]:
    """Compose the activated catalog from SQLite config, folds, and economics.

    Finished rows' whole-life results are not read here: that read is the
    costly one, and ``with_finished_results`` runs it off the event loop.
    """
    identified_statuses = [require_sqlite_catalog_identity(status) for status in statuses]
    _require_one_catalog_economic_revision(
        identified_statuses,
        projections,
        economic_rollups,
        account_id=account_id,
    )
    rows = [
        compose_catalog_view(
            status,
            sqlite_catalog_rollup(economic_rollups[status.strategy_instance_id]),
            account_id=account_id,
            world=home.world,
            holds_money=status.strategy_instance_id in home.holding_money,
            latest_stop_ms=home.latest_stops.get(status.strategy_instance_id),
        )
        for status in statuses
    ]
    return adapt_sqlite_catalog(rows, projections, economic_rollups)


async def with_finished_results(
    rows: list[BotCatalogView],
    read_results: Callable[[Sequence[str]], dict[str, BotResult]],
) -> list[BotCatalogView]:
    """Give each Finished row its whole life: its executions and its result.

    Read only when a row is Finished, in a worker thread: the lifetime fee
    projection is blocking work and never runs on the event loop
    (``ClerkSqliteRepository.bot_results`` reuses it at an unchanged custody
    revision). That memo is the repository's and holds ONE entry, keyed by
    ``(control_revision, Finished sids)``: a read whose Finished set differs
    from the previous read's recomputes rather than reuses.

    When the fee evidence cannot vouch for a result (``BudgetUnavailable``)
    the rows keep it unknown -- said in the log, never shown as $0 -- and the
    roster still renders. Any other error from the results read is unexpected
    and propagates: it refuses the whole catalog read, by design, rather than
    render every row beside Finished results that failed for an unnamed reason.
    """
    finished = [row.strategy_instance_id for row in rows if row.group == "finished"]
    if not finished:
        return rows
    try:
        results = await asyncio.to_thread(read_results, finished)
    except BudgetUnavailable:
        logger.warning(
            "Finished bots' results are unavailable; the rows show them as unknown",
            exc_info=True, extra={"action": "finished_results_unavailable", "bots": len(finished)},
        )
        return rows
    return [
        row if row.group != "finished" else row.with_facts(
            trade_count=results[row.strategy_instance_id].trade_count,
            final_result_usd=display_dollars(results[row.strategy_instance_id].result),
        )
        for row in rows
    ]


def _require_one_catalog_economic_revision(
    statuses: list[BotStatusView],
    projections: dict[str, ClerkProjection],
    economic_rollups: dict[str, EconomicSnapshot],
    *,
    account_id: str,
) -> None:
    """Prove the roster's economics came from one account/revision snapshot."""
    snapshots: list[EconomicSnapshot] = []
    for status in statuses:
        snapshot = economic_rollups.get(status.strategy_instance_id)
        if snapshot is None:
            raise SqliteCatalogProjectionUnavailable(
                f"Bot '{status.strategy_instance_id}' has no SQLite economic snapshot."
            )
        if (
            snapshot.strategy_instance_id != status.strategy_instance_id
            or snapshot.account_id != account_id
        ):
            raise SqliteCatalogProjectionUnavailable(
                "SQLite catalog economics do not match the requested bot/account."
            )
        projection = projections.get(status.strategy_instance_id)
        if projection is None:
            # A retired row proved inert before this read skipped its custody
            # projection (#1911); every other row must have one, and a missing
            # projection there is still a torn read.
            if status.phase != "RETIRED":
                raise SqliteCatalogProjectionUnavailable(
                    f"Bot '{status.strategy_instance_id}' has no SQLite custody projection."
                )
            snapshots.append(snapshot)
            continue
        if (
            projection.account_id != snapshot.account_id
            or projection.strategy_instance_id != snapshot.strategy_instance_id
            or projection.authority_generation != snapshot.authority_generation
            or projection.control_revision != snapshot.control_revision
        ):
            raise SqliteCatalogRevisionMismatch(
                "SQLite catalog custody and economic folds do not share one authority revision."
            )
        snapshots.append(snapshot)
    revisions = {
        (snapshot.authority_generation, snapshot.control_revision)
        for snapshot in snapshots
    }
    if len(revisions) > 1:
        raise SqliteCatalogProjectionUnavailable(
            "SQLite catalog economics span multiple authority revisions."
        )


#: Status labels whose lifecycle already proved an unclean terminal exit.
#: Read from the shared vocabulary rather than restated, so the labels this
#: adapter defers to and the labels ``status_label_for`` emits cannot drift.
_UNCLEAN_EXIT_STATUS_LABELS = frozenset(
    copy_for(key).label for key in ("CRASHED", "EXITED_UNVERIFIED")
)


def _sqlite_catalog_explanation(
    row: BotCatalogView,
    projection: ClerkProjection,
    *,
    exposure: dict[str, float],
) -> str:
    """Re-author the row explanation with SQLite-clerk evidence.

    ``status_label`` is lifecycle-derived and this explanation is
    clerk-derived, and nothing used to reconcile them: an unclean terminal
    exit already carries a canonical explanation from
    ``status_explanation_for``, which this adapter then overwrote with an
    ordinary off-duty string. A row labelled "Crashed" read "Off duty and
    flat." beside it -- two authors, one of them wrong (#1806).

    So the unclean-exit case defers to the incoming explanation rather than
    restating it here, unless the bot still holds money: then the row says
    what it holds and that no bot manages it (PRD #2560 D7), which a crash
    label beside it never contradicts. The copy names no internal authority.
    """
    held = holdings_text(exposure)
    if row.running:
        return f"Running · holds {held}" if held else "Running · no position"
    # What a stopped bot still holds is its row's headline on Home (PRD
    # #2560 D7), whatever else is true of it: an unclean label beside it
    # names how the run ended, and nothing here contradicts that.
    if held:
        return f"Stopped · still holds {held} · no bot is managing it"
    if row.group == "holding":
        return "Stopped · an entry order is still working · no bot is managing it"
    if _clerk_needs_attention(projection):
        return "Its order records need attention."
    if row.status_label in _UNCLEAN_EXIT_STATUS_LABELS:
        return row.status_explanation
    if row.phase == "RETIRED":
        return "Retired; no further runs can start."
    return "Off duty and flat."


def _panel_action(
    capability: RecoveryCapability,
    revision: int,
    *,
    flatten_verdict: SessionAuthorityState | LegRefusal | None = None,
) -> PanelAction:
    if not capability.available:
        blocker: OperatorBlocker | None = _capability_blocker(capability)
    elif capability.action_id == "execute_safe_flatten":
        blocker = _flatten_session_blocker(flatten_verdict)
    else:
        blocker = None
    confirmation = capability.confirmation
    return PanelAction(
        action_id=capability.action_id,
        label=capability.label,
        explanation=capability.explanation,
        enabled=blocker is None,
        blockers=[] if blocker is None else [blocker],
        confirmation=(
            None
            if confirmation is None
            else OperatorConfirmationCopy(
                title=confirmation.title,
                body=capability.explanation,
                consequence=confirmation.explanation,
                confirm_label=confirmation.confirm_label,
            )
        ),
        revision=revision,
        concurrency_token=capability.concurrency_token,
        evidence_refs=[evidence.reference for evidence in capability.evidence],
        needed=capability.needed,
    )


# The bot cockpit's own reconcile control, named the way the account desk
# names its equivalents. The shared blocker list renders any
# ``confirm_in_form`` move; the host decides what the anchor opens.
BOT_COCKPIT_RECONCILE_ANCHOR = "bot-reconciliation-action"
# The cockpit's own Prepare safe flatten control: where the live bid and ask
# are shown and an extended-hours limit is confirmed (#2007).
BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR = "bot-safe-flatten-prepare"

_RECONCILE_MOVE = OperatorMove(
    label="Reconcile this account now",
    action=ConfirmInFormAction(
        kind="confirm_in_form", anchor=BOT_COCKPIT_RECONCILE_ANCHOR
    ),
)


_PREPARE_PRICED_FLATTEN_MOVE = OperatorMove(
    label="Prepare safe flatten",
    action=ConfirmInFormAction(
        kind="confirm_in_form", anchor=BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR
    ),
)


_PREPARED_PLAN_SHOWS_WHEN = "Prepare safe flatten shows when."


def _flatten_session_blocker(
    verdict: SessionAuthorityState | LegRefusal | None,
) -> OperatorBlocker | None:
    """Why the generic flatten button cannot send now, or ``None`` inside the regular session.

    This button sends a flatten with no price. Outside 09:30-16:00 there is
    none to send (#2007, owner decisions 2026-09-19): in PRE/POST a flatten is
    a limit the operator prices from the live bid and ask in the prepared
    plan, which sends it through the custody route; with no session this
    Clerk can price in, nothing is sent at all. The Clerk refuses both at
    execution regardless -- this only keeps the button from offering a click
    that cannot succeed.

    ``verdict`` is the Clerk's own answer (``recovery_reduction.flatten_send_verdict``),
    so a refusal is shown in the Clerk's words -- ``NO_SESSION_OPEN``, or
    ``EXTENDED_HOURS_PRICING_UNAVAILABLE`` when the calendar's after-hours is
    open on an authority that declares no extended window (#2440 review) --
    never a second reading of the session made here. The blocker list renders
    no time, so a refusal that names one points at the prepared plan, which
    shows it (``available_at_ms``, kept as evidence).
    A ``None`` verdict means no authority answered. That is not an RTH
    fallback: the one session in which this button can succeed is the only
    one it may assume, so an unanswered session blocks it too (CodeRabbit
    review 2026-09-19).
    """
    if verdict is None:
        return OperatorBlocker.for_host(
            condition_id="FLATTEN_SESSION_UNKNOWN",
            scope="bot",
            host="bot_cockpit",
            anchor=SURFACE_ANCHOR,
            disposition="fix_here",
            headline="The trading session is unknown, so an unpriced flatten cannot be sent.",
            detail=(
                "Prepare safe flatten reads the session and the live IBKR bid and ask, and "
                "says what can be sent now."
            ),
            applies_to="run",
            primary_move=_PREPARE_PRICED_FLATTEN_MOVE,
        )
    if isinstance(verdict, LegRefusal):
        return OperatorBlocker.for_host(
            condition_id=verdict.reason_code,
            scope="bot",
            host="bot_cockpit",
            anchor=SURFACE_ANCHOR,
            disposition="wait",
            headline=verdict.explanation,
            detail=(
                verdict.next_step
                if verdict.available_at_ms is None
                else f"{verdict.next_step} {_PREPARED_PLAN_SHOWS_WHEN}"
            ),
            applies_to="run",
            evidence={"available_at_ms": verdict.available_at_ms},
        )
    if verdict.phase == "RTH":
        return None
    return OperatorBlocker.for_host(
        condition_id="EXTENDED_HOURS_FLATTEN_NEEDS_A_LIMIT",
        scope="bot",
        host="bot_cockpit",
        anchor=SURFACE_ANCHOR,
        disposition="fix_here",
        headline="Outside regular hours a flatten is a limit order you price.",
        detail=(
            "Prepare safe flatten to see the live IBKR bid and ask, then send the limit "
            "from the prepared plan."
        ),
        applies_to="run",
        primary_move=_PREPARE_PRICED_FLATTEN_MOVE,
        evidence={"session_phase": verdict.phase},
    )


def _capability_blocker(capability: RecoveryCapability) -> OperatorBlocker:
    """Author one unavailable recovery capability as operator guidance.

    Disposition follows what the operator can actually do about it. Stale
    evidence is curable *here* -- reconciling refreshes it -- so it carries
    the reconcile move. Everything else genuinely is a wait, and `wait`
    correctly renders no move; authoring stale evidence as `wait` was
    violating that contract rather than expressing it (S17).
    """
    curable_here = capability.freshness == "stale"
    return OperatorBlocker.for_host(
        condition_id=capability.unavailable_reason_code or "RECOVERY_ACTION_UNAVAILABLE",
        scope="bot" if capability.scope == "CUSTODY_SUBJECT" else "account",
        host="bot_cockpit",
        anchor=SURFACE_ANCHOR,
        disposition="fix_here" if curable_here else "wait",
        headline=capability.unavailable_reason or "This recovery action is unavailable.",
        detail=capability.next_step,
        applies_to="run",
        primary_move=_RECONCILE_MOVE if curable_here else None,
        evidence={
            "freshness": capability.freshness,
            "evidence_count": len(capability.evidence),
        },
    )


def _readiness_check(
    capability: RecoveryCapability,
    evaluated_at_ms: int,
) -> ReadinessCheckView:
    return ReadinessCheckView(
        operation=capability.action_id,
        label=capability.label,
        ready=capability.available,
        scope="bot" if capability.scope == "CUSTODY_SUBJECT" else "account",
        authority="SQLite Account Clerk recovery policy",
        explanation=(
            capability.explanation
            if capability.available
            else capability.unavailable_reason or capability.explanation
        ),
        evidence={
            "freshness": capability.freshness,
            "evidence_count": len(capability.evidence),
            "primary": capability.primary,
        },
        evaluated_at_ms=evaluated_at_ms,
        cure=None if capability.available else capability.next_step,
    )


def _mission_verdict(
    panel: BotPanelView,
    projection: ClerkProjection,
) -> MissionVerdictView:
    guidance = projection.guidance
    if _clerk_needs_attention(projection):
        state = "blocked"
        label = "Mission blocked"
    elif panel.health.running:
        state = "working"
        label = "Working"
    elif panel.health.phase == "RETIRED":
        state = "retired"
        label = "Retired"
    else:
        state = "off_duty"
        label = "Off duty"
    return MissionVerdictView(
        state=state,
        label=label,
        explanation=guidance.explanation,
        next_action=guidance.next_step,
        evaluated_at_ms=projection.generated_at_ms,
        recovery_status=guidance.recovery_status,
    )


def _clerk_needs_attention(projection: ClerkProjection) -> bool:
    """Whether this SQLite cut asks for the operator: unhealthy authority, an uncertainty, or a hold."""
    return projection.authority_health != "healthy" or bool(projection.uncertainties) or bool(projection.holds)


def _clerk_vouches_for_positions(projection: ClerkProjection) -> bool:
    """Whether the Clerk's attributed positions can be taken as what the bot holds.

    Narrower than ``_clerk_needs_attention``: a hold blocks new exposure but
    leaves what is held known, while an unhealthy authority or an open
    uncertainty means the attribution itself may be wrong.
    """
    reconciliation = projection.latest_reconciliation
    return (
        projection.authority_health == "healthy" and not projection.uncertainties
        and reconciliation is not None and reconciliation.outcome == "RESOLVED_SUCCESS"
        and reconciliation.effect_operation_id is None and reconciliation.order_ref is None
        and 0 <= projection.generated_at_ms - reconciliation.attempted_at_ms <= FRESH_EVIDENCE_MAX_AGE_MS
    )


def _is_working(order: ProjectedOrder) -> bool:
    """An order the broker acknowledged and has not finished."""
    return order.broker_order_id is not None and (order.broker_state or "").lower() in _WORKING_BROKER_STATES


def _may_still_fill(order: ProjectedOrder) -> bool:
    """Any order that has not ended, including one the broker has not yet acknowledged.

    Wider than the working-order list on purpose: ``held``, ``pending_replace``,
    ``accepted_for_bidding`` and an order captured but not yet acknowledged can
    all still fill into a position the refused run will not manage. The one
    definition (``order_projection.ORDER_OPEN_SQL``) is read with the order.
    """
    return order.may_fill


def _with_terminal_exposure_notices(panel: BotPanelView, projection: ClerkProjection) -> BotHealthCard:
    health = panel.health
    outcome = health.duty_outcome
    if outcome is None:
        return health
    notices = terminal_exposure_notices(
        projection, sid=panel.strategy_instance_id, symbol=panel.symbol,
        kind=outcome.kind, reason_code=outcome.reason_code, running=health.running,
    )
    return health.model_copy(update={"duty_outcome": outcome.model_copy(update={"exposure_notices": notices})})


def terminal_exposure_notices(
    projection: ClerkProjection, *, sid: str, symbol: str, kind: str, reason_code: str, running: bool,
) -> list[ExposureNoticeView]:
    """One backend-authored warning set shared by panel, account desk and bell."""
    if running or kind not in UNCLEAN_DUTY_OUTCOMES:
        return []
    notices: list[ExposureNoticeView] = []
    if not _clerk_vouches_for_positions(projection):
        notices.append(_POSITION_UNVERIFIED)
    else:
        held = [
            position
            for position in projection.positions
            if position.strategy_instance_id == sid and position_quantity_is_nonzero(position.attributed_qty)
        ]
        if held:
            positions = ", ".join(f"{position.attributed_qty:g} {position.symbol}" for position in held)
            closing = (
                "The Clerk is still working this bot's exit order; Flatten becomes available "
                "if that order ends without closing the position."
                if exit_in_progress(projection, sid)
                else "Use Flatten to close this position."
            )
            notices.append(
                ExposureNoticeView(
                    kind="position_unmanaged",
                    label="Bot is not managing this position",
                    explanation=(
                        f"The Clerk attributes {positions} to this bot. The run has ended and "
                        f"will not make further decisions. {closing}"
                    ),
                )
            )
    if any(
        order.role == "ENTRY" and _may_still_fill(order)
        for operation in projection.operations
        if operation.strategy_instance_id == sid
        for order in operation.orders
    ):
        notices.append(_ENTRY_ORDER_WORKING)
    return [notice.model_copy(update={
        "strategy_instance_id": sid, "symbol": symbol,
        "action_label": "Flatten" if notice.kind == "position_unmanaged" else "Open bot",
    }) for notice in notices]


def exit_in_progress(projection: ClerkProjection, sid: str) -> bool:
    """The Clerk keeps working an ended run's exit (#2504), so Flatten waits for it."""
    return any(
        operation.kind == "EXIT" and operation.state in NONTERMINAL_EFFECT_STATES
        for operation in projection.operations
        if operation.strategy_instance_id == sid
    )


# Every fix it names is on the bot's own page (hurdle H29): it never sends
# the owner to the broker outside the app.
_POSITION_UNVERIFIED = ExposureNoticeView(
    kind="position_unverified",
    label="Position could not be verified",
    explanation=(
        "The app cannot currently vouch for what this bot holds: its custody record is not "
        "healthy, or an account or order state is uncertain. The ended run manages nothing. "
        "Reconcile now re-reads the account at Alpaca; Flatten becomes available once the "
        "position is proven."
    ),
)
_ENTRY_ORDER_WORKING = ExposureNoticeView(
    kind="entry_order_working",
    label="An entry order is still working",
    explanation=(
        "An entry order this bot placed is still working at the broker. The Clerk is "
        "cancelling it; if it fills first, the ended run will not manage the position it opens."
    ),
)


def _working_orders(
    panel: BotPanelView,
    projection: ClerkProjection,
    *,
    strict: bool,
) -> list[WorkingOrderView]:
    return [
        _working_order_view(panel, order, strict=strict)
        for operation in projection.operations
        for order in operation.orders
        if _is_working(order)
    ]


def _working_order_view(
    panel: BotPanelView,
    order: ProjectedOrder,
    *,
    strict: bool,
) -> WorkingOrderView:
    """Shape one durable working order without consulting broker or JSONL."""
    if strict and (
        order.symbol is None
        or order.side is None
        or order.quantity is None
        or order.filled_quantity is None
    ):
        raise SqlitePanelAdapterUnavailable(
            f"SQLite order {order.order_ref!r} lacks durable leg or fill evidence"
        )
    return WorkingOrderView(
        order_ref=order.order_ref,
        broker_order_id=order.broker_order_id or "",
        symbol=order.symbol or panel.symbol,
        side=order.side or "unavailable",
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        status=order.broker_state or "unknown",
        observed_at_ms=order.updated_at_ms,
    )


def recent_fill_view(
    fill: FillRecord,
    *,
    authority_account_id: str,
    account_mode: Literal["paper", "live"],
    repository: ClerkSqliteRepository | None = None,
) -> RecentFillView:
    """Adapt one S2 fill record to the existing panel wire contract.

    Stamps the authority the fill was actually read from (#1729 AC #8). This
    adapter overwrites whatever ``recent_fills`` the projector produced, so
    without the stamp here a production fill row reaches the panel carrying no
    authority at all while its sibling decision row carries one — the exact
    asymmetry the single-authority guard exists to make impossible.

    A fill of a priced reducing leg — an operator's confirmed extended-hours
    flatten (#2007), or a limit the Clerk priced itself (#2229, #2440) — also
    carries its realized slippage from the quote the limit was priced against.
    """
    kind = authority_kind_for_account(authority_account_id, account_mode=account_mode)
    reference_price = (
        None
        if repository is None
        else priced_reduction_reference_price(repository, fill.order_ref)
    )
    return RecentFillView(
        order_ref=fill.order_ref,
        symbol=fill.symbol,
        side=fill.side.value,
        quantity=fill.quantity,
        price=fill.fill_price,
        filled_at_ms=fill.filled_at_ms,
        simulated=kind in SIMULATED_AUTHORITY_KINDS,
        authority_account_id=authority_account_id,
        authority_kind=kind,
        event_key=fill.event_key,
        slippage_reference_price=reference_price,
        slippage_bps=(
            None
            if reference_price is None
            else realized_slippage_bps(
                side=fill.side, reference_price=reference_price, fill_price=fill.fill_price
            )
        ),
        slippage_cost=(
            None
            if reference_price is None
            else realized_slippage_cost(
                side=fill.side,
                reference_price=reference_price,
                fill_price=fill.fill_price,
                quantity=fill.quantity,
            )
        ),
    )


class SqlitePanelAdapterUnavailable(RuntimeError):
    """The active SQLite panel cannot safely present an incomplete fold."""


def _require_coherent_economic_snapshot(
    projection: ClerkProjection,
    economics: EconomicSnapshot,
) -> None:
    if (
        economics.account_id != projection.account_id
        or economics.strategy_instance_id != projection.strategy_instance_id
        or economics.authority_generation != projection.authority_generation
        or economics.control_revision != projection.control_revision
    ):
        raise SqlitePanelAdapterUnavailable(
            "SQLite custody and economic projections do not share one authority revision"
        )


@dataclass(frozen=True)
class _ResolvedOrderEvidence:
    """Just enough order evidence for the rail, sourced outside the bounded window."""

    broker_order_id: str | None
    broker_state: str | None


@dataclass(frozen=True)
class _ResolvedOperationEvidence:
    """One effect operation's rail-relevant evidence, resolved by exact stored key.

    Mirrors the subset of ``ProjectedOperation`` the rail actually reads.
    Built from unbounded exact-key repository reads
    (``ClerkSqliteRepository.effect_operation`` / ``.order`` /
    ``.orders_for_effect_operation`` — already used throughout the SQLite
    Clerk, e.g. ``exit_resolution.py``, ``reconcile.py``), never from a
    hand-rolled second copy of the fold's operation-assembly query.
    """

    effect_operation_id: str
    state: str
    terminal_receipt_id: str | None
    orders: tuple[_ResolvedOrderEvidence, ...]


@dataclass(frozen=True)
class _OperationSelection:
    """The outcome of resolving one ``selected_ref`` against custody evidence.

    ``unresolved_ref`` is set only when a caller-supplied ``selected_ref``
    matched nothing anywhere — neither the bounded projection window nor an
    unbounded stored-key lookup. That is a materially different state from
    "no ref was requested" (``operation`` simply defaults to the most recent)
    and must render as explicit, named absence rather than silently
    substituting another transaction (issue #1729 AC #6/#7).
    """

    operation: ProjectedOperation | _ResolvedOperationEvidence | None
    unresolved_ref: str | None


def _operation_matches_ref(operation: ProjectedOperation, ref: str) -> bool:
    return operation.effect_operation_id == ref or any(
        order.order_ref == ref for order in operation.orders
    )


def _resolve_operation_outside_window(
    repository: ClerkSqliteRepository,
    projection: ClerkProjection,
    ref: str,
) -> _ResolvedOperationEvidence | None:
    """Stored-key join for a ref absent from the bounded operation window.

    ``ref`` may name an effect operation directly or one of its orders — the
    same two identities ``_operation_matches_ref`` checks in-window. Resolved
    evidence is discarded (treated as not found) unless it belongs to the
    bot this projection is scoped to; a real ref from a *different* bot must
    not leak into this bot's rail (mirrors
    ``station_derivation._validate_transaction_ownership``).
    """
    order = repository.order(ref)
    effect_operation_id = order.effect_operation_id if order is not None else ref
    effect = repository.effect_operation(effect_operation_id)
    if effect is None or effect.strategy_instance_id != projection.strategy_instance_id:
        return None
    orders = repository.orders_for_effect_operation(effect_operation_id)
    return _ResolvedOperationEvidence(
        effect_operation_id=effect.effect_operation_id,
        state=effect.state,
        terminal_receipt_id=effect.terminal_receipt_id,
        orders=tuple(
            _ResolvedOrderEvidence(broker_order_id=o.broker_order_id, broker_state=o.broker_state)
            for o in orders
        ),
    )


def _select_operation(
    projection: ClerkProjection,
    selected_ref: str | None,
    *,
    repository: ClerkSqliteRepository | None,
) -> _OperationSelection:
    if selected_ref is None:
        latest = projection.operations[0] if projection.operations else None
        return _OperationSelection(operation=latest, unresolved_ref=None)
    for operation in projection.operations:
        if _operation_matches_ref(operation, selected_ref):
            return _OperationSelection(operation=operation, unresolved_ref=None)
    resolved = (
        _resolve_operation_outside_window(repository, projection, selected_ref)
        if repository is not None
        else None
    )
    if resolved is not None:
        return _OperationSelection(operation=resolved, unresolved_ref=None)
    return _OperationSelection(operation=None, unresolved_ref=selected_ref)


def _transaction_rail(
    projection: ClerkProjection,
    *,
    selected_ref: str | None,
    repository: ClerkSqliteRepository | None = None,
) -> TransactionRail:
    selection = _select_operation(projection, selected_ref, repository=repository)
    if selection.unresolved_ref is not None:
        # Explicit, named absence: the requested transaction does
        # not exist anywhere in this bot's durable custody evidence. Echo the
        # searched ref back rather than substituting another transaction.
        return TransactionRail(
            transaction_ref=selection.unresolved_ref,
            stations=[
                _station(
                    station_id,
                    "not_applicable",
                    f"No custody operation or order matches transaction "
                    f"{selection.unresolved_ref!r}.",
                )
                for station_id in _station_ids()
            ],
        )
    operation = selection.operation
    if operation is None:
        return TransactionRail(
            transaction_ref=None,
            stations=[_station(station_id, "not_applicable", "No custody operation selected.") for station_id in _station_ids()],
        )
    has_broker_ack = any(order.broker_order_id is not None for order in operation.orders)
    has_fill_evidence = any(
        (order.broker_state or "").lower() in _FILLED_BROKER_STATES for order in operation.orders
    )
    reconciled = (
        operation.terminal_receipt_id is not None
        or (
            projection.latest_reconciliation is not None
            and projection.latest_reconciliation.effect_operation_id
            == operation.effect_operation_id
            and projection.latest_reconciliation.outcome == "RESOLVED_SUCCESS"
        )
    )
    stations = [
        _station("SIGNAL", "satisfied", "The durable command records the requested operation."),
        _station("INTENT", "satisfied", "The Clerk captured the operation before broker contact."),
        _station("SUBMIT_GATE", "satisfied", "SQLite custody accepted the operation under its active authority."),
        _station(
            "BROKER_ACK",
            "satisfied" if has_broker_ack else ("unknown_stale" if operation.state == "unknown" else "waiting"),
            "A broker order identity is durably linked." if has_broker_ack else "No broker acknowledgment is durably linked yet.",
        ),
        _station(
            "FILL",
            "satisfied" if has_fill_evidence else ("unknown_stale" if operation.state == "unknown" else "waiting"),
            "A broker fill or partial fill is durably recorded." if has_fill_evidence else "No fill has been recorded for this order.",
        ),
        _station(
            "RECONCILED",
            "satisfied" if reconciled else ("unknown_stale" if operation.state == "unknown" else "waiting"),
            "Terminal or reconciliation evidence closes the custody chain." if reconciled else "Awaiting terminal or reconciliation evidence.",
        ),
    ]
    return TransactionRail(
        transaction_ref=operation.effect_operation_id,
        stations=stations,
    )


def _station_ids() -> tuple[str, ...]:
    return ("SIGNAL", "INTENT", "SUBMIT_GATE", "BROKER_ACK", "FILL", "RECONCILED")


def _station(station_id: str, state: str, receipt: str) -> StationView:
    station_copy = copy_for(station_id)
    state_copy = copy_for(state)
    return StationView(
        station_id=station_id,
        label=station_copy.label,
        state=state,
        state_label=state_copy.label,
        receipt=receipt,
        evidence_at_ms=None,
        blocker=None,
    )


__all__ = [
    "SQLITE_PANEL_LIFECYCLE_ACTION_IDS",
    "adapt_sqlite_catalog",
    "adapt_sqlite_panel",
    "build_sqlite_catalog",
    "exit_in_progress",
    "has_bot_scoped_custody_problem",
    "recent_fill_view",
]
