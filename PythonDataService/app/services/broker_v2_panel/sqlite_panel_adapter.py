"""Adapt SQLite Clerk folds to the existing Broker V2 panel contract.

The panel remains the product surface.  This module is only a projection
adapter: it neither replays transitions nor authors a second recovery policy.
Every action and action token comes from the SQLite recovery catalog.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.alpaca.clerk.account_authority import authority_kind_for_account
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.recovery_reduction import (
    realized_slippage_bps,
    realized_slippage_cost,
)
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.exit_resolution import priced_reduction_reference_price
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ProjectedOperation,
    ProjectedOrder,
    RecoveryCapability,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import UNCONDITIONAL_RECOVERY_ACTION_IDS
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.v2panel.vocabulary import copy_for
from app.marketdata.feed import WARMUP_REFUSAL_REASONS
from app.schemas.account_authority import SIMULATED_AUTHORITY_KINDS
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
from app.services.broker_v2_panel.panel_projection_service import select_primary_action_by_lens
from app.services.session_authority import TRADEABLE_EXTENDED_PHASES, TradingSessionPhase

_WORKING_BROKER_STATES = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "pending_cancel"}
)
# Matches the established station-derivation semantic (`station_derivation.py`
# `_fill_station`): FILL is satisfied only by actual fill evidence, never by
# any terminal outcome — a canceled/expired/rejected order definitively
# received zero fill and must not render as satisfied.
_FILLED_BROKER_STATES = frozenset({"filled", "partially_filled"})

# SQLite owns broker-recovery actions after activation, but bot-lifecycle
# actions remain the runner's.  Preserve them from the generic panel policy
# instead of trying to reconstruct their guards from the custody projection.
#
# Membership does double duty: it is also what routes a POST past the SQLite
# recovery executor to the generic performer (`sqlite_panel_source`).  An
# action omitted here is silently deleted on the way to Angular however
# completely its guard, performer and lens are wired -- which is exactly what
# happened to `retire` (#1778, S5).
SQLITE_PANEL_LIFECYCLE_ACTION_IDS = frozenset({"resume", "retire", "archive"})


def adapt_sqlite_panel(
    panel: BotPanelView,
    projection: ClerkProjection,
    *,
    economics: EconomicSnapshot | None = None,
    repository: ClerkSqliteRepository | None = None,
    flatten_phase: TradingSessionPhase | None = None,
) -> BotPanelView:
    """Replace JSONL-derived custody fields with one SQLite fold snapshot.

    Active callers supply ``economics`` at the identical SQLite revision as
    ``projection``.  The optional form remains only for narrow historical
    adapter tests; the active source fails closed before calling this adapter
    without authoritative economic evidence.

    ``repository`` is optional and used only as the PRD Sec 19 stored-key
    fallback when the selected transaction ref is absent from the bounded
    ``projection.operations`` window (§7.1's 50/100-row cap).  Without it, a
    ref outside the window renders as "not found" rather than being resolved.

    ``flatten_phase`` is the session an operator's flatten would go out in now
    (``SqliteAlpacaClerkFacade.flatten_session``). Outside the regular session
    the generic Execute safe flatten button is not the way to flatten (#2007):
    see ``_flatten_session_blocker``.
    """
    if economics is not None:
        _require_coherent_economic_snapshot(projection, economics)
    lifecycle_actions = [
        action.model_copy(update={"revision": projection.control_revision})
        for action in panel.actions
        if (
            action.action_id in SQLITE_PANEL_LIFECYCLE_ACTION_IDS
            and not panel.health.running
        )
    ]
    actions = [
        *lifecycle_actions,
        *(
            _panel_action(item, projection.control_revision, flatten_phase=flatten_phase)
            for item in projection.recovery_actions
            if item.action_id not in SQLITE_PANEL_LIFECYCLE_ACTION_IDS
        ),
    ]
    checks = [_readiness_check(item, projection.generated_at_ms) for item in projection.recovery_actions]
    ready_count = sum(check.ready for check in checks)
    recovery_primary_action_id = next(
        (item.action_id for item in projection.recovery_actions if item.primary),
        None,
    )
    return panel.model_copy(
        update={
            "health": _with_startup_refusal_notices(panel, projection),
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
            "primary_action_by_lens": select_primary_action_by_lens(
                actions,
                panel.health,
                recovery_primary_action_id=recovery_primary_action_id,
            ),
            "readiness_checks": checks,
            "readiness_ready_count": ready_count,
            "readiness_blocked_count": len(checks) - ready_count,
            "exposure": {
                position.symbol: position.attributed_qty
                for position in projection.positions
                if position_quantity_is_nonzero(position.attributed_qty)
            },
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
                    _recent_fill_view(
                        fill,
                        authority_account_id=projection.account_id,
                        repository=repository,
                    )
                    for fill in economics.recent_fills
                ]
            ),
            "fills_today": None if economics is None else economics.fills_today,
            "realized_pnl_today": (
                None if economics is None else economics.realized_pnl_today
            ),
            "open_pnl": None if economics is None else economics.open_pnl,
        }
    )


def _has_bot_scoped_custody_problem(projection: ClerkProjection) -> bool:
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
    family as the account-wide entry freeze this PRD exists to remove. Such a
    row still reads ``needs_attention`` -- it is genuinely affected -- it just
    offers no button.

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
    bot_scoped_custody_problem = _has_bot_scoped_custody_problem(projection)
    if not row_needs_attention and not bot_scoped_custody_problem:
        return None
    primary = next(
        (item for item in projection.recovery_actions if item.primary),
        None,
    )
    if primary is None:
        return None
    if primary.action_id in UNCONDITIONAL_RECOVERY_ACTION_IDS and not bot_scoped_custody_problem:
        return None
    return _panel_action(primary, projection.control_revision)


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


def build_sqlite_catalog(
    statuses: list[BotStatusView],
    projections: dict[str, ClerkProjection],
    *,
    economic_rollups: dict[str, EconomicSnapshot],
    account_id: str,
) -> list[BotCatalogView]:
    """Compose the activated catalog from SQLite config, folds, and economics."""
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
        )
        for status in statuses
    ]
    return adapt_sqlite_catalog(rows, projections, economic_rollups)


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
    restating it here. Both off-duty strings are guarded, not just the flat
    one: "Off duty with Clerk-attributed exposure." contradicts a crash label
    exactly as much, and the exposure itself stays visible in ``exposure``
    and ``needs_attention``.
    """
    if _clerk_needs_attention(projection):
        return "SQLite Account Clerk evidence requires operator attention."
    if row.phase == "RETIRED":
        return "Retired; no further runs can start."
    if row.running:
        return "Running under SQLite Account Clerk custody."
    if row.status_label in _UNCLEAN_EXIT_STATUS_LABELS:
        return row.status_explanation
    if any(position_quantity_is_nonzero(quantity) for quantity in exposure.values()):
        return "Off duty with Clerk-attributed exposure."
    return "Off duty and flat."


def _panel_action(
    capability: RecoveryCapability,
    revision: int,
    *,
    flatten_phase: TradingSessionPhase | None = None,
) -> PanelAction:
    if not capability.available:
        blocker: OperatorBlocker | None = _capability_blocker(capability)
    elif capability.action_id == "execute_safe_flatten":
        blocker = _flatten_session_blocker(flatten_phase)
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


def _flatten_session_blocker(phase: TradingSessionPhase | None) -> OperatorBlocker | None:
    """Why the generic flatten button cannot send now, or ``None`` inside the regular session.

    This button sends a flatten with no price. Outside 09:30-16:00 there is
    none to send (#2007, owner decisions 2026-09-19): in PRE/POST a flatten is
    a limit the operator prices from the live bid and ask in the prepared
    plan, which sends it through the custody route; with no session open,
    nothing is sent at all. The Clerk refuses both at execution regardless --
    this only keeps the button from offering a click that cannot succeed.
    A ``None`` phase means no authority answered. That is not an RTH
    fallback: the one session in which this button can succeed is the only
    one it may assume, so an unanswered session blocks it too (CodeRabbit
    review 2026-09-19).
    """
    if phase == "RTH":
        return None
    if phase is None:
        return OperatorBlocker.for_host(
            condition_id="FLATTEN_SESSION_UNKNOWN",
            scope="bot",
            host="bot_cockpit",
            anchor=SURFACE_ANCHOR,
            audience="both",
            disposition="fix_here",
            headline="The trading session is unknown, so an unpriced flatten cannot be sent.",
            detail=(
                "Prepare safe flatten reads the session and the live IBKR bid and ask, and "
                "says what can be sent now."
            ),
            applies_to="run",
            primary_move=_PREPARE_PRICED_FLATTEN_MOVE,
        )
    priced_here = phase in TRADEABLE_EXTENDED_PHASES
    return OperatorBlocker.for_host(
        condition_id=(
            "EXTENDED_HOURS_FLATTEN_NEEDS_A_LIMIT" if priced_here else "NO_SESSION_OPEN"
        ),
        scope="bot",
        host="bot_cockpit",
        anchor=SURFACE_ANCHOR,
        audience="both",
        disposition="fix_here" if priced_here else "wait",
        headline=(
            "Outside regular hours a flatten is a limit order you price."
            if priced_here
            else "No trading session is open now, so no flatten can be sent."
        ),
        detail=(
            "Prepare safe flatten to see the live IBKR bid and ask, then send the limit "
            "from the prepared plan."
            if priced_here
            else "Flatten once the next session opens; Prepare safe flatten shows when."
        ),
        applies_to="run",
        primary_move=_PREPARE_PRICED_FLATTEN_MOVE if priced_here else None,
        evidence={"session_phase": phase},
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
        audience="both",
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
        next_attempt_at_ms=guidance.next_attempt_at_ms,
        next_attempt_overdue=guidance.next_attempt_overdue,
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
    return projection.authority_health == "healthy" and not projection.uncertainties


def _is_working(order: ProjectedOrder) -> bool:
    """An order the broker acknowledged and has not finished."""
    return order.broker_order_id is not None and (order.broker_state or "").lower() in _WORKING_BROKER_STATES


_TERMINAL_BROKER_STATES = frozenset({"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"})


def _may_still_fill(order: ProjectedOrder) -> bool:
    """Any order the broker has not finished, including one it has not yet acknowledged.

    Wider than the working-order list on purpose: ``held``, ``pending_replace``,
    ``accepted_for_bidding`` and an order captured but not yet acknowledged can
    all still fill into a position the refused run will not manage.
    """
    return (order.broker_state or "").lower() not in _TERMINAL_BROKER_STATES


def _with_startup_refusal_notices(panel: BotPanelView, projection: ClerkProjection) -> BotHealthCard:
    """Say what a startup refusal left at the broker, from this SQLite cut (#2410).

    A run refused while it prepared never managed anything. The owner's rule:
    say so whenever money can still move. The Clerk's attributed position is
    trusted only when it can vouch for it (``_clerk_vouches_for_positions``);
    otherwise the position is reported unverified rather than guessed. A
    working entry order is reported separately, because a flat bot can still
    be filled into a position nobody manages. Nothing is cancelled or
    flattened here.
    """
    health = panel.health
    outcome = health.duty_outcome
    if health.running or outcome is None or outcome.reason_code not in WARMUP_REFUSAL_REASONS:
        return health
    sid = panel.strategy_instance_id
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
            notices.append(
                ExposureNoticeView(
                    kind="position_unmanaged",
                    label="Bot is not managing this position",
                    explanation=(
                        f"The Clerk attributes {positions} to this bot. The refused run placed no "
                        "exit and will not place one: manage or close the position from the "
                        "broker, or resume once the refusal's cause is fixed."
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
    if not notices:
        return health
    return health.model_copy(
        update={"duty_outcome": outcome.model_copy(update={"exposure_notices": notices})}
    )


_POSITION_UNVERIFIED = ExposureNoticeView(
    kind="position_unverified",
    label="Position could not be verified; check the broker",
    explanation=(
        "The Clerk cannot currently vouch for what this bot holds -- its authority is not "
        "healthy or an account or order state is uncertain. The refused run manages nothing, "
        "so check the position and any working orders at the broker."
    ),
)
_ENTRY_ORDER_WORKING = ExposureNoticeView(
    kind="entry_order_working",
    label="An entry order is still working",
    explanation=(
        "An entry order this bot placed is still working at the broker. If it fills, the "
        "refused bot will not manage the position it opens. Cancel it if you do not want it."
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


def _recent_fill_view(
    fill: FillRecord,
    *,
    authority_account_id: str,
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
    kind = authority_kind_for_account(authority_account_id)
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

    Mirrors the subset of ``ProjectedOperation`` the rail actually reads
    (§7.1). Built from unbounded exact-key repository reads
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
    substituting another transaction (PRD Sec 19; issue #1729 AC #6/#7).
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
        # Explicit, named absence (PRD Sec 19): the requested transaction does
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
]
