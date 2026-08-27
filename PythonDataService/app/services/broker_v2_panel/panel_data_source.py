"""Panel data-source facade (spec §3, §5, §7, §8, §11).

Resolves the live dependencies the account-scoped panel endpoints need — the
account id, the order journal, the decision journals, the bot roster, the clerk
status, and the live chart aggregator — and delegates every computation to the
pure projection functions. The router stays transport-only; this facade is the
single seam that touches process singletons.
Account scope (§3): every method validates ``account_id`` against the broker's
real account and raises :class:`AccountMismatchError` (→ 404) on a mismatch, so
a stale deep link never reads another account's evidence.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.models import (
    EffectOperationState,
    EffectPurpose,
    OrderJournalEntry,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ExecutionLeaseLost,
    RepositoryPoisoned,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.ibkr.config import live_artifacts_root
from app.schemas.broker_bots import (
    BotControlAuthorityFacts,
    BotStatusView,
)
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    ChartHistoryResponse,
    ChartHistoryTimeframe,
    ChartLiveResponse,
    PanelActionRequest,
    PanelActionResult,
)
from app.schemas.run_admission import ProgramBuildAdmissionFact, RunAdmissionDecision
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    live_state_binding_repository,
)
from app.services.bot_runner import (
    ActivationFailedCleanupProvenError,
    BotRunnerError,
    get_bot_task_registry,
)
from app.services.bot_start_admission import market_data_capability_account_id
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    ActionPerformer,
    ActivationFailedError,
    AuthorityPoisonedError,
    ExecutionAuthorityLostError,
    durable_idempotency_store_for,
    execute_action,
)
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
)
from app.services.broker_v2_panel.market_pulse import build_market_pulse
from app.services.broker_v2_panel.panel_errors import (
    PanelUnavailableError,
    UnknownBotError,
)
from app.services.broker_v2_panel.panel_profile_service import panel_profile_for
from app.services.broker_v2_panel.panel_projection_service import (
    build_panel,
    program_build_view_from_run_evidence,
)
from app.services.broker_v2_panel.panel_scope import (
    bot_process_fact,
    clerk_status,
    validate_account,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    adapt_sqlite_panel,
)
from app.services.broker_v2_panel.sqlite_panel_source import (
    SqlitePanelBotNotFound,
    SqlitePanelDecisionUnavailable,
    SqlitePanelEconomicUnavailable,
    execute_sqlite_panel_action,
    read_sqlite_catalog,
    read_sqlite_catalog_from_facade,
    read_sqlite_decision_receipts,
    read_sqlite_panel_evidence,
)
from app.services.market_data_capability_service import get_market_data_capability_service
from app.services.signal_program_admission import prove_running_program_build
from app.services.sqlite_clerk_compat import active_sqlite_facade
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _panel_authority_for_binding(
    registry: object,
    binding: BrokerBotBinding,
) -> AsyncIterator[SqliteAlpacaClerkFacade | None]:
    """Select the Clerk authority named by one durable bot binding.

    The request account remains the real operator account used for route
    authorization. Dry Run custody is intentionally stored under a separate
    ``sim:<strategy_instance_id>`` account and must be selected explicitly for
    its evidence reads.
    """
    if getattr(binding, "mode", None) != "dry_run":
        yield None
        return
    projection_runtime = getattr(registry, "synthetic_runtime_for_projection", None)
    if not callable(projection_runtime):
        raise PanelUnavailableError(
            "The Dry Run custody authority is unavailable.",
            detail="The bot runner cannot compose the sealed synthetic Clerk for this projection.",
        )
    async with projection_runtime(binding) as runtime:
        facade = runtime.clerk
        if not isinstance(facade, SqliteAlpacaClerkFacade) or runtime.authority_kind != "synthetic":
            raise PanelUnavailableError(
                "The Dry Run custody authority is unavailable.",
                detail="The sealed synthetic Clerk did not provide a SQLite projection authority.",
            )
        yield facade


def _run_evidence_repository() -> BotBindingRepository:
    """Bind the shared ``live_state`` repository factory to this service's
    artifacts root — the same root ``main.py`` wires ``BotTaskRegistry``
    from (``live_artifacts_root()``), so a read here never drifts
    from the in-container bot runner's own view.
    """
    return live_state_binding_repository(live_artifacts_root())


def _program_build_for_display(
    binding: BrokerBotBinding,
    *,
    verified_at_ms: int,
) -> ProgramBuildAdmissionFact:
    """PRD Sec 11.3 run evidence: what was actually proven for THIS run.

    Prefers the durable per-run record written at Start/Resume
    (``BotBindingRepository.read_program_build_evidence``) over a fresh
    ``prove_running_program_build`` re-check, so the panel never shows a
    verdict that drifted from what was actually admitted underfoot. Falls
    back to the live proof only when no per-run evidence was ever recorded
    — a build that closed UNPROVEN/NOT_APPLICABLE at Start writes no
    evidence file, and runs launched before #1728 predate this capture.
    """
    evidence = _run_evidence_repository().read_program_build_evidence(
        binding.strategy_instance_id, binding.run_id
    )
    if evidence is not None:
        return program_build_view_from_run_evidence(binding.strategy_key, evidence)
    return prove_running_program_build(binding, verified_at_ms=verified_at_ms)


async def get_authority_facts(
    broker: str,
    account_id: str,
    sid: str,
) -> BotControlAuthorityFacts:
    """Compose owner-authored facts without deriving a control decision."""
    resolved_account_id = await validate_account(broker, account_id)
    process = bot_process_fact(broker, sid)
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise PanelUnavailableError(
            "Alpaca order management is not configured.",
            detail="The Clerk cannot author current custody facts.",
        )
    custody = await clerk.custody_snapshot(sid)
    if custody.account_id != resolved_account_id:
        raise PanelUnavailableError(
            "The Clerk custody account does not match the panel account.",
            detail="Recover the account-scoped Clerk before using control actions.",
        )
    return BotControlAuthorityFacts(process=process, clerk=custody)


async def get_catalog(broker: str, account_id: str) -> list[BotCatalogView]:
    """Build the bots-list catalog for one account (§5)."""
    resolved = await validate_account(broker, account_id)
    try:
        sqlite_catalog = await read_sqlite_catalog(broker, resolved)
    except SqliteCatalogProjectionUnavailable as exc:
        raise PanelUnavailableError(
            "The activated SQLite bot roster could not be projected.",
            detail=str(exc),
        ) from exc
    if sqlite_catalog is None:
        raise PanelUnavailableError(
            "The activated SQLite bot roster is unavailable.",
            detail="Restore or reactivate the account-scoped SQLite authority.",
        )
    registry = get_bot_task_registry()
    if registry is None:
        return sqlite_catalog
    synthetic_rows: list[BotCatalogView] = []
    for binding in registry.bindings_for_broker(broker):
        if binding.mode != "dry_run":
            continue
        try:
            async with _panel_authority_for_binding(registry, binding) as facade:
                assert facade is not None
                rows = await read_sqlite_catalog_from_facade(broker, facade)
        except SqliteCatalogProjectionUnavailable as exc:
            raise PanelUnavailableError(
                "The sealed Dry Run roster could not be projected.",
                detail=str(exc),
            ) from exc
        synthetic_rows.extend(
            row.model_copy(update={"mode": "dry_run"})
            for row in rows
            if row.strategy_instance_id == binding.strategy_instance_id
        )
    return [*sqlite_catalog, *synthetic_rows]


async def _get_panel_with_entries_from_authority(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolved: str,
    captured_now_ms: int,
    registry: object,
    binding: object,
    facade: SqliteAlpacaClerkFacade | None,
    transaction_ref: str | None = None,
) -> tuple[BotPanelView, list[OrderJournalEntry], tuple[FillRecord, ...] | None]:
    """Build one panel from its binding-selected SQLite authority."""
    authority_account_id = facade.account_id if facade is not None else resolved
    try:
        evidence = await read_sqlite_panel_evidence(
            broker,
            authority_account_id,
            sid,
            now_ms=captured_now_ms,
            facade=facade,
        )
    except SqlitePanelBotNotFound as exc:
        raise UnknownBotError(str(exc)) from exc
    except SqlitePanelEconomicUnavailable as exc:
        raise PanelUnavailableError(
            "The activated SQLite panel evidence is unavailable.",
            detail=str(exc),
        ) from exc
    if evidence is None:
        raise PanelUnavailableError(
            "The activated SQLite panel authority is unavailable.",
            detail="Restore or reactivate the account-scoped SQLite authority.",
        )
    status = _status_in_binding_mode(evidence.status, binding)
    resume_admission: RunAdmissionDecision | None = None
    if not status.running:
        try:
            # Resume is the only action this projection informs, so skip it
            # entirely for a live run. The preview itself is a pure read: it
            # projects the sweep's verdict rather than reconciling (#1776).
            resume_admission = await registry.preview_resume_admission(broker, sid)
        except BotRunnerError as exc:
            resume_admission = exc.admission_decision
            if resume_admission is None:
                logger.warning(
                    "broker panel Resume admission is unavailable",
                    extra={"broker": broker, "account_id": account_id, "sid": sid, "detail": exc.detail},
                )
        # No second evidence cut: the preview projects the sweep's last
        # verdict and mutates nothing (#1776 WP2), so the evidence read above
        # is already this response's single consistent cut. Re-reading here
        # doubled the cost of every stopped bot's poll.
    projection = evidence.projection
    session_fills = evidence.economics.session_fills
    entries: list[OrderJournalEntry] = []
    clerk = await clerk_status(symbol=binding.symbol)
    try:
        decision_read = read_sqlite_decision_receipts(broker, sid, facade=facade)
    except SqlitePanelDecisionUnavailable as exc:
        raise PanelUnavailableError(
            "The activated SQLite decision evidence is unavailable.",
            detail=str(exc),
        ) from exc
    if decision_read is None:
        raise PanelUnavailableError(
            "The activated SQLite decision evidence is unavailable.",
            detail="The SQLite authority became unavailable during panel projection.",
        )
    decisions = decision_read
    decision = decisions[-1] if decisions else None
    economics = evidence.economics.snapshot

    # PRD Sec 11.3/11.4 run evidence: prefer the exact build durably recorded
    # for THIS run at Start/Resume over a fresh re-check, which can drift
    # from what actually started running if the manifest or artifacts change
    # underfoot afterwards (#1728 Gap 2). Falls back to the same canonical
    # proof Start/Resume admission uses only when no per-run evidence was
    # ever recorded (never PROVEN at Start, or a run that predates it).
    program_build = _program_build_for_display(binding, verified_at_ms=captured_now_ms)

    profile = panel_profile_for(broker)
    flatten_supported = profile.flatten_supported if profile is not None else False
    from app.marketdata.ibkr_feed import get_market_data_feed

    market_data_feed = get_market_data_feed()
    capability_account_id = market_data_capability_account_id(market_data_feed)
    panel = build_panel(
        status,
        clerk,
        entries,
        account_id=resolved,
        authority_account_id=authority_account_id,
        exposure=dict(economics.exposure),
        fills_today=economics.fills_today,
        realized_pnl_today=economics.realized_pnl_today,
        open_pnl=economics.open_pnl,
        latest_decision=decision,
        last_bar_at_ms=economics.last_activity_at_ms,
        journal_tail_ref=f"/api/brokers/{broker}/accounts/{resolved}/bots/{sid}/decisions",
        journal_tail_seq=(decision.seq if decision is not None else None),
        flatten_supported=flatten_supported,
        now_ms=captured_now_ms,
        selected_transaction_ref=transaction_ref,
        recent_decisions=decisions,
        resume_admission=resume_admission,
        sealed_program=binding.sealed_program,
        program_build=program_build,
        dry_run_activity=registry.dry_run_activity(broker, sid),
        market_pulse=build_market_pulse(
            market_data_feed,
            now_ms=captured_now_ms,
            symbol=binding.symbol,
            account_id=capability_account_id,
            capability=(
                get_market_data_capability_service().read_latest_for(
                    symbol=binding.symbol,
                    account_id=capability_account_id,
                )
                if capability_account_id is not None
                else None
            ),
            use_rth=binding.use_rth,
            bot_running=status.running,
        ),
    )
    # The transaction-rail stored-key fallback (PRD Sec 19, issue #1729 AC #6/#7)
    # needs the same repository `read_sqlite_panel_evidence` resolved internally
    # for its reads; `facade` above can be `None` here for the primary (non
    # Dry Run) authority, so re-resolve it the same way rather than skipping
    # the fallback for the majority of real bots.
    rail_facade = facade or active_sqlite_facade(broker)
    panel = adapt_sqlite_panel(
        panel,
        projection,
        economics=economics,
        repository=rail_facade.repository if rail_facade is not None else None,
    )
    return panel, entries, session_fills


def _status_in_binding_mode(status: BotStatusView, binding: BrokerBotBinding) -> BotStatusView:
    """Retain authority-owned lifecycle facts while exposing the sealed mode."""
    if getattr(binding, "mode", None) != "dry_run":
        return status
    return status.model_copy(
        update={
            "mode": "dry_run",
            "quantity": binding.quantity,
            "carryover_policy": binding.carryover_policy,
        }
    )


async def _get_panel_with_entries(
    broker: str,
    account_id: str,
    sid: str,
    *,
    transaction_ref: str | None = None,
    now_ms: int | None = None,
) -> tuple[BotPanelView, list[OrderJournalEntry], tuple[FillRecord, ...] | None]:
    """Build one SQLite-backed panel and return its exact chart fill set."""
    resolved = await validate_account(broker, account_id)
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    binding = registry.binding_for_control(broker, sid)
    captured_now_ms = now_ms if now_ms is not None else now_ms_utc()
    async with _panel_authority_for_binding(registry, binding) as facade:
        return await _get_panel_with_entries_from_authority(
            broker,
            account_id,
            sid,
            resolved=resolved,
            captured_now_ms=captured_now_ms,
            registry=registry,
            binding=binding,
            facade=facade,
            transaction_ref=transaction_ref,
        )


async def get_panel_with_chart_fills(
    broker: str,
    account_id: str,
    sid: str,
    *,
    now_ms: int,
) -> tuple[BotPanelView, list[OrderJournalEntry], tuple[FillRecord, ...] | None]:
    return await _get_panel_with_entries(broker, account_id, sid, now_ms=now_ms)


async def get_panel(
    broker: str,
    account_id: str,
    sid: str,
    *,
    transaction_ref: str | None = None,
) -> BotPanelView:
    """Build the current panel projection for one bot (§7)."""
    panel, _entries, _session_fills = await _get_panel_with_entries(
        broker,
        account_id,
        sid,
        transaction_ref=transaction_ref,
    )
    return panel


async def get_live_chart(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"] = "1m",
) -> ChartLiveResponse:
    """Build the LIVE chart pane for one bot (§8)."""
    from app.services.broker_v2_panel.panel_chart_data_source import (
        get_live_chart as build_live_chart_response,
    )

    return await build_live_chart_response(
        broker,
        account_id,
        sid,
        resolution=resolution,
    )


async def get_live_snapshot_parts(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"],
) -> tuple[BotPanelView, ChartLiveResponse]:
    """Build panel and chart from the same authority-bound fill projection."""
    from app.services.broker_v2_panel.panel_chart_data_source import (
        get_live_snapshot_parts as build_live_snapshot_parts,
    )

    panel, chart = await build_live_snapshot_parts(
        broker,
        account_id,
        sid,
        resolution=resolution,
    )
    assert isinstance(panel, BotPanelView)
    return panel, chart


async def get_history_chart(
    broker: str,
    account_id: str,
    sid: str,
    timeframe: ChartHistoryTimeframe,
) -> ChartHistoryResponse:
    """Build the bounded HISTORY chart pane for one bot (§8)."""
    from app.services.broker_v2_panel.panel_chart_data_source import (
        get_history_chart as build_history_chart_response,
    )

    return await build_history_chart_response(broker, account_id, sid, timeframe)


def _action_performers(broker: str, sid: str, *, idempotency_key: str) -> dict[str, ActionPerformer]:
    """Map each executable action id to the coroutine that performs it (§11, §12).

    Only actions with production custody are wired. The remaining closed-set
    actions raise ``ActionNotAvailableError`` from the executor rather than
    presenting a fake success.
    """

    async def _resume(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        try:
            admitted = await registry.resume_existing_with_admission(broker, sid)
        except ActivationFailedCleanupProvenError as exc:
            raise ActivationFailedError(
                f"Resume for run '{exc.attempted_run_id}' failed after Clerk "
                "registration; the Clerk stop committed.",
                detail=exc.detail or str(exc),
            ) from exc
        except BotRunnerError as exc:
            raise ActionNotAvailableError(
                "Resume is no longer available for this bot.",
                detail=exc.detail or str(exc),
                reason_code=(
                    exc.admission_decision.reason_code
                    if exc.admission_decision is not None
                    else None
                ),
            ) from exc
        return (
            f"Bot resumed as new run {admitted.bot.active_run_id} from its immutable configuration. "
            "The Clerk remains the only owner of broker order effects."
        )

    async def _stop(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        await registry.stop(broker, sid, reason=f"Panel stop by {operator}")
        return "Bot stopped. The Clerk cancelled any working entry orders; attributed exposure was left untouched."

    async def _retire(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        await registry.retire(
            broker,
            sid,
            updated_by=operator,
            reason=f"Panel retire by {operator}",
        )
        return (
            "Registration retired and taken off the roster. It issues no further "
            "feed subscriptions and can start no new runs."
        )

    async def _pause(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        status = await registry.pause(
            broker,
            sid,
            updated_by=operator,
            reason=f"Panel pause by {operator}",
        )
        return f"Run {status.active_run_id} is paused; its process remains live."

    async def _continue(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        status = await registry.continue_paused(
            broker,
            sid,
            updated_by=operator,
            reason=f"Panel Continue by {operator}",
        )
        return f"Run {status.active_run_id} continued without changing run identity."

    async def _reconcile(operator: str, reason: str | None) -> str:
        clerk = get_alpaca_clerk()
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        verdict = await clerk.reconcile_once()
        return f"Reconciliation sweep complete: {verdict}."

    async def _flatten_stop(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        clerk = get_alpaca_clerk()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        binding = registry.binding_for_control(broker, sid)
        status = registry.status(broker, sid)
        if status.running:
            # STOP-AND-FLATTEN is ordered custody: first persist STOPPED and
            # cancel strategy evaluation/working entries, then derive the
            # reducing EXIT. A failed flatten must never leave the strategy
            # running or able to submit a fresh entry.
            await registry.stop(
                broker,
                sid,
                reason=f"Panel flatten-and-stop by {operator}",
            )
        receipt = await clerk.execute_for_instance(
            strategy_instance_id=sid,
            run_id=binding.run_id,
            decision_id=f"panel-flatten:{idempotency_key}",
            purpose=EffectPurpose.EXIT,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
        )
        if receipt.state is EffectOperationState.UNPROVABLE:
            return (
                "The bot is stopped, but the Clerk cannot prove that attributed exposure "
                "is flat. Inspect the Clerk receipt before issuing another action."
            )
        if receipt.state is EffectOperationState.FLAT:
            return "The Clerk proved attributed exposure is flat and the bot is stopped."
        return (
            "The bot is stopped and the Clerk submitted the reducing operation; "
            "await its durable fill receipt before treating exposure as flat."
        )

    return {
        "resume": _resume,
        "pause": _pause,
        "continue": _continue,
        "stop": _stop,
        "retire": _retire,
        "flatten_stop": _flatten_stop,
        "reconcile_now": _reconcile,
    }


async def run_action(
    broker: str,
    account_id: str,
    sid: str,
    request: PanelActionRequest,
    *,
    operator_identity: str,
) -> PanelActionResult:
    """Execute one presented action for a bot (§11).

    Recomputes the current panel revision (the guard the POST is checked
    against), then delegates to the execution service. Identity is the
    configured ``operator_identity`` — never a request field.
    """
    try:
        return await _run_action_under_live_authority(
            broker, account_id, sid, request, operator_identity=operator_identity
        )
    except (ExecutionLeaseLost, RepositoryPoisoned) as error:
        # T7c (#1794): this account's authority cannot be written to. Refusing
        # is correct; leaking the internal handle message as a raw 500 is not.
        # Translated here rather than in the router so SQLite repository
        # internals stay behind this seam.
        #
        # The two are kept apart because their cures differ: a lost lease means
        # another process owns the account and a restart re-acquires it; a
        # poisoned repository means this authority's own last transition is
        # unproven until the fence reconciliation re-runs. Collapsing them
        # would hand an operator the wrong remedy.
        lost_lease = isinstance(error, ExecutionLeaseLost)
        logger.warning(
            "Panel action refused: account authority unavailable",
            extra={
                "action": (
                    "panel_action_execution_authority_lost"
                    if lost_lease
                    else "panel_action_authority_poisoned"
                ),
                "broker": broker,
                "account_id": account_id,
                "strategy_instance_id": sid,
                "action_id": request.action_id,
                "error": str(error),
            },
        )
        raise (
            ExecutionAuthorityLostError() if lost_lease else AuthorityPoisonedError()
        ) from error


async def _run_action_under_live_authority(
    broker: str,
    account_id: str,
    sid: str,
    request: PanelActionRequest,
    *,
    operator_identity: str,
) -> PanelActionResult:
    panel = await get_panel(broker, account_id, sid)
    action = next(
        (candidate for candidate in panel.actions if candidate.action_id == request.action_id),
        None,
    )
    if action is None:
        raise UnknownBotError(
            f"Action '{request.action_id}' is not available for bot '{sid}'.",
            detail="Refresh the panel before retrying the command.",
        )
    availability_error: ActionNotAvailableError | None = None
    if not action.enabled:
        blocker = action.blockers[0] if action.blockers else None
        availability_error = ActionNotAvailableError(
            f"The '{action.label}' action is blocked by the current panel state.",
            detail=(
                blocker.detail
                if blocker is not None
                else "Refresh the panel and inspect the operation's readiness check."
            ),
        )
    registry = get_bot_task_registry()
    try:
        sqlite_result = await execute_sqlite_panel_action(
            broker,
            account_id,
            sid,
            request=request,
            panel=panel,
            action=action,
            availability_error=availability_error,
            # Same durable receipt ledger the shared executor uses, so a
            # repost of an applied SQLite recovery action replays as a no-op
            # instead of re-executing (fleet run 2026-08-25 / F15).
            store=(
                durable_idempotency_store_for(registry.panel_action_receipt_path(sid))
                if registry is not None
                else None
            ),
        )
    except SqlitePanelBotNotFound as exc:
        raise UnknownBotError(str(exc)) from exc
    if sqlite_result is not None:
        return sqlite_result
    if registry is None:
        raise PanelUnavailableError("The bot runner is not available.")
    return await execute_action(
        request,
        sid=sid,
        current_revision=panel.revision,
        current_concurrency_token=action.concurrency_token,
        performers=_action_performers(broker, sid, idempotency_key=request.idempotency_key),
        operator_identity=operator_identity,
        store=durable_idempotency_store_for(registry.panel_action_receipt_path(sid)),
        availability_error=availability_error,
    )
