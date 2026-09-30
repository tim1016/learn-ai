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
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, NoReturn

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import (
    account_route_matches_custody,
    evidence_account_id_for,
)
from app.broker.alpaca.clerk.active_authority import (
    primary_custody_world,
)
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.models import (
    OrderJournalEntry,
    ReconciliationCut,
)
from app.broker.alpaca.clerk.money import dollars
from app.broker.alpaca.clerk.sqlite.repository import (
    ExecutionLeaseLost,
    RepositoryPoisoned,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.ibkr.config import live_artifacts_root
from app.engine.live.identity import INSTANCE_ID_PATTERN
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
from app.schemas.run_admission import ProgramBuildAdmissionFact
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    live_state_binding_repository,
)
from app.services.bot_runner import (
    BotTaskRegistry,
    get_bot_task_registry,
)
from app.services.bot_runner_errors import BotRunnerError, InvalidStrategyInstanceIdError
from app.services.bot_runner_errors import UnknownBotError as RunnerUnknownBotError
from app.services.bot_start_admission import market_data_capability_account_id
from app.services.broker_v2_panel.action_execution_service import (
    REVIVAL_OUTCOME_AUTHORITY_UNAVAILABLE,
    REVIVAL_OUTCOME_NO_SWEEP,
    REVIVAL_OUTCOME_REFUSED,
    REVIVAL_OUTCOME_TRANSIENT_STORE_ERROR,
    REVIVAL_REMEDY_TRANSIENT_STORE_ERROR,
    ActionNotAvailableError,
    ActionPerformer,
    AuthorityPoisonedError,
    DryRunAuthorityLeaseLostError,
    ExecutionAuthorityLostError,
    ExecutionAuthorityRevivedError,
    durable_idempotency_store_for,
    execute_action,
)
from app.services.broker_v2_panel.bot_custody import binding_clerk_runtime, custody_facade
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    custody_bot_status,
)
from app.services.broker_v2_panel.market_pulse import build_market_pulse
from app.services.broker_v2_panel.panel_errors import (
    PanelUnavailableError,
    UnknownBotError,
)
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
from app.services.source_bar_ledger import (
    RetainedContinuityEvent,
    RetainedStartupJoin,
    RetainedWarmupJoin,
    SourceBarLedger,
    SourceBarLedgerCorruptError,
    SourceBarLedgerMissingError,
)
from app.services.sqlite_clerk_compat import (
    active_reconciliation_sweep,
    active_sqlite_facade,
    custody_account_id_for_route,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _panel_authority_for_binding(
    registry: object,
    binding: BrokerBotBinding,
) -> AsyncIterator[SqliteAlpacaClerkFacade | None]:
    """The Clerk facade one durable bot binding runs on (``bot_custody``'s selection).

    The request account remains the real operator account used for route
    authorization; what this yields is the authority the binding actually
    runs on, whose account id may differ from it. Dry Run custody is stored
    under a separate ``sim:<strategy_instance_id>`` account; a shadow
    authority custodies ``shadow:<live_account_id>`` while the route names
    the live account. Under real paper the two are the same id, so yielding
    the facade is behaviour-identical there — and under shadow it is what
    keeps the evidence read addressed at the repository it came from and
    every synthesized fill labelled ``simulated`` (ADR 0059 D2, ruling R8).
    """
    async with binding_clerk_runtime(registry, binding) as runtime:
        yield custody_facade(runtime)


@asynccontextmanager
async def _selected_panel_authority(
    broker: str, account_id: str, sid: str,
) -> AsyncIterator[tuple[str, BotTaskRegistry, BrokerBotBinding, SqliteAlpacaClerkFacade | None]]:
    """Authorize the route account, then hold the bot's one custody selection open.

    Yields the resolved account id, the registry, the bot's binding and the
    selected facade, so a read and the action it gates project from the same
    authority, selected once.

    An id the runner holds no binding for -- or could never hold one for --
    is the panel's own ``UnknownBotError`` (404) in the runner's words: the
    runner's error type is not a panel error, so untranslated it answered
    the bot page with a 500 and aborted a whole clear batch at one leg.
    """
    resolved = await validate_account(broker, account_id)
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    try:
        binding = registry.binding_for_control(broker, sid)
    except (RunnerUnknownBotError, InvalidStrategyInstanceIdError) as exc:
        raise UnknownBotError(str(exc), detail=exc.detail) from exc
    async with _panel_authority_for_binding(registry, binding) as facade:
        yield resolved, registry, binding, facade


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


@dataclass(frozen=True)
class RunSourceEvidence:
    """One run's source-stream facts the panel shows, read at one open of its ledger."""

    events: list[RetainedContinuityEvent]
    warmup_join: RetainedWarmupJoin | None
    startup_join: RetainedStartupJoin | None


def _run_source_evidence_for(binding: BrokerBotBinding) -> RunSourceEvidence | None:
    """Read this binding's durable current-run continuity facts and warmup join.

    ``None`` is an explicit unavailable state (mode retains no source bars,
    ledger absent, ledger unreadable, or no primary custody authority
    installed to resolve the evidence namespace from). An empty ``events``
    means the run's ledger exists and has recorded no interruptions; a
    ``None`` ``warmup_join`` means the run did not resume on retained bars
    (#2314), or has not reached warmup yet.

    This is a read path, not a start path: unlike
    ``bot_binding_authority.primary_custody_kind`` (which refuses a *start*
    when no authority is installed), a panel read degrades to the
    documented ``None`` instead of raising ``StartAdmissionUnavailable`` —
    a missing authority must not take down an otherwise-servable panel.
    """
    if binding.mode not in {"dry_run", "trade"}:
        return None
    if binding.mode == "dry_run":
        custody_kind = "synthetic"
    else:
        custody_kind = primary_custody_world()
        if custody_kind is None:
            return None
    evidence_account_id = evidence_account_id_for(
        mode=binding.mode,
        strategy_instance_id=binding.strategy_instance_id,
        custody_kind=custody_kind,
    )
    try:
        ledger = SourceBarLedger(
            artifacts_root=live_artifacts_root(),
            account_id=evidence_account_id,
            read_only=True,
        )
        try:
            return RunSourceEvidence(
                events=ledger.events(run_id=binding.run_id),
                warmup_join=ledger.warmup_join(run_id=binding.run_id),
                startup_join=ledger.startup_join(run_id=binding.run_id),
            )
        finally:
            ledger.close(checkpoint=False)
    except SourceBarLedgerMissingError:
        return None
    except (SourceBarLedgerCorruptError, sqlite3.Error, OSError) as exc:
        logger.warning(
            "Bot panel continuity evidence is unavailable",
            extra={
                "action": "panel_feed_continuity_unavailable",
                "strategy_instance_id": binding.strategy_instance_id,
                "run_id": binding.run_id,
                "reason": str(exc),
            },
        )
        return None


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
    if custody.account_id != custody_account_id_for_route(broker, resolved_account_id):
        raise PanelUnavailableError(
            "The Clerk custody account does not match the panel account.",
            detail="Recover the account-scoped Clerk before using control actions.",
        )
    return BotControlAuthorityFacts(process=process, clerk=custody)


async def get_catalog(broker: str, account_id: str) -> list[BotCatalogView]:
    """Build the bots-list catalog for one account (§5)."""
    resolved = await validate_account(broker, account_id)
    try:
        sqlite_catalog = await read_sqlite_catalog(
            broker, custody_account_id_for_route(broker, resolved)
        )
    except SqliteCatalogProjectionUnavailable as exc:
        raise PanelUnavailableError(
            "This account's bot roster could not be read.",
            detail=str(exc),
        ) from exc
    if sqlite_catalog is None:
        raise PanelUnavailableError(
            "This account's bot roster is unavailable.",
            detail="Restore the account's Clerk, then refresh.",
        )
    registry = get_bot_task_registry()
    if registry is None:
        return sqlite_catalog
    synthetic_rows: list[BotCatalogView] = []
    for binding in registry.bindings_for_broker(broker):
        if binding.mode != "dry_run":
            continue
        # A cleared Dry Run leaves Home and this poll: its sealed simulator is
        # never opened again for it (#2567). Clearing needed it stopped and
        # flat, so there is nothing left in it to show.
        if registry.status(broker, binding.strategy_instance_id).phase == "RETIRED":
            continue
        try:
            async with _panel_authority_for_binding(registry, binding) as facade:
                assert facade is not None
                rows = await read_sqlite_catalog_from_facade(broker, facade)
                budget = facade.repository.deployment_budget(binding.strategy_instance_id)
        except SqliteCatalogProjectionUnavailable as exc:
            raise PanelUnavailableError(
                "The sealed Dry Run roster could not be projected.",
                detail=str(exc),
            ) from exc
        # A Dry Run's starting cash is its consent amount, private to its
        # simulated account (PRD #2540); a pre-budget Dry Run has none. Only
        # the Dry Run group carries it: a stopped, flat Dry Run is Finished
        # (#2567), and ``model_copy`` skips the schema check that says so.
        simulated_cash = None if budget is None else dollars(budget["committed_cents"])
        synthetic_rows.extend(
            row.model_copy(update={
                "mode": "dry_run",
                "simulated_cash_usd": simulated_cash if row.group == "dry_run" else None,
            })
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
    if facade is None:
        raise PanelUnavailableError(
            "This bot's Clerk is unavailable.",
            detail="Restore the selected authority before projecting this bot's execution policy.",
        )
    authority_account_id = facade.account_id
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
            "This bot's custody and money could not be read together.",
            detail=str(exc),
        ) from exc
    if evidence is None:
        raise PanelUnavailableError(
            "This bot's Clerk is unavailable.",
            detail="Restore the account's Clerk, then refresh.",
        )
    status = _status_in_binding_mode(evidence.status, binding)
    projection = evidence.projection
    session_fills = evidence.economics.session_fills
    entries: list[OrderJournalEntry] = []
    # The card names the authority the evidence came from: a Dry Run's own
    # simulated account, never the real account it is listed under (H33).
    clerk = await clerk_status(symbol=binding.symbol, facade=facade)
    try:
        decision_read = read_sqlite_decision_receipts(broker, sid, facade=facade)
    except SqlitePanelDecisionUnavailable as exc:
        raise PanelUnavailableError(
            "This bot's decision record is unavailable.",
            detail=str(exc),
        ) from exc
    if decision_read is None:
        raise PanelUnavailableError(
            "This bot's decision record is unavailable.",
            detail="Its Clerk became unavailable while the page was read.",
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

    from app.marketdata.ibkr_feed import get_market_data_feed

    market_data_feed = get_market_data_feed()
    capability_account_id = market_data_capability_account_id(market_data_feed)
    source_evidence = _run_source_evidence_for(binding)
    panel = build_panel(
        status,
        clerk,
        entries,
        bot_status=custody_bot_status(facade.repository, sid, running=status.running),
        account_id=resolved,
        authority_account_id=authority_account_id,
        exposure=dict(economics.exposure),
        fills_today=economics.fills_today,
        realized_pnl_today=economics.realized_pnl_today,
        exact_open_pnl=economics.exact_open_pnl,
        latest_decision=decision,
        last_bar_at_ms=economics.last_activity_at_ms,
        journal_tail_ref=f"/api/brokers/{broker}/accounts/{resolved}/bots/{sid}/decisions",
        journal_tail_seq=(decision.seq if decision is not None else None),
        now_ms=captured_now_ms,
        selected_transaction_ref=transaction_ref,
        recent_decisions=decisions,
        sealed_program=binding.sealed_program,
        program_build=program_build,
        dry_run_activity=registry.dry_run_activity(broker, sid),
        # The owner's end lives in the runner's desired state, not in custody (#2607).
        end=registry.bot_end(broker, sid),
        market_pulse=build_market_pulse(
            market_data_feed,
            # Captured after every await above, not at request start: the
            # broker clock re-stamps on its own ~1 s poller while this
            # projection awaits, so the request-start instant can predate the
            # evidence and read as a future-dated MARKET_CLOCK_INVALID on a
            # healthy feed (#2256). Start and Resume admission re-capture for
            # the same reason.
            now_ms=now_ms_utc(),
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
            extended_window=facade.program_leg_policy.window,
        ),
        feed_continuity_events=None if source_evidence is None else source_evidence.events,
        feed_continuity_run_id=binding.run_id,
        warmup_join=None if source_evidence is None else source_evidence.warmup_join,
        startup_join=None if source_evidence is None else source_evidence.startup_join,
    )
    # The rail reads the same binding-selected repository and send policy.
    panel = adapt_sqlite_panel(
        panel,
        projection,
        economics=economics,
        repository=facade.repository,
        flatten_verdict=facade.flatten_send_verdict(),

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
    captured_now_ms = now_ms if now_ms is not None else now_ms_utc()
    async with _selected_panel_authority(broker, account_id, sid) as (resolved, registry, binding, facade):
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


def _action_performers(
    broker: str,
    sid: str,
    *,
    reconciled: ReconciliationCut | None = None,
) -> dict[str, ActionPerformer]:
    """Map each executable lifecycle action id to the coroutine that performs it (§11, §12).

    Only Archive (Clear) reaches this executor: every other presented action
    is the SQLite recovery catalog's and runs through
    ``execute_sqlite_panel_action``. ``reconciled`` is a clear batch's one
    account pass, which archive's guard may answer against
    (``bot_runner.archive``).
    """

    async def _archive(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        try:
            await registry.archive(
                broker,
                sid,
                updated_by=operator,
                # The operator's own words when they gave any; the generic line is
                # a fallback, not a replacement for the audit context they typed.
                reason=reason or f"Panel archive by {operator}",
                reconciled=reconciled,
            )
        except BotRunnerError as error:
            # The commit-time guard refused under the bot's lock, before any
            # write (ADR 0052 §3): a typed refusal that names its cause, not
            # an unknown outcome -- nothing was applied.
            raise ActionNotAvailableError(str(error), detail=error.detail, reason_code=error.reason_code) from error
        return (
            "Bot archived and taken off the roster. Its history and receipts are "
            "kept; it can start no new runs."
        )

    return {"archive": _archive}


async def run_action(
    broker: str,
    account_id: str,
    sid: str,
    request: PanelActionRequest,
    *,
    operator_identity: str,
    reconciled: ReconciliationCut | None = None,
) -> PanelActionResult:
    """Execute one presented action for a bot (§11).

    Recomputes the current panel revision (the guard the POST is checked
    against), then delegates to the execution service. Identity is the
    configured ``operator_identity`` — never a request field.

    ``sid`` reaches the durable panel-action receipt path, so the canonical
    instance-id pattern is enforced here — the one funnel every action
    ``sid`` crosses (direct panel posts and cohort legs alike) — and a
    malformed id is a clean 404, not a filesystem read. The artifact-path
    builders apply the same pattern again; this guard is the boundary copy.
    """
    if re.fullmatch(INSTANCE_ID_PATTERN, sid) is None:
        raise UnknownBotError(
            f"Bot id '{sid}' is not a valid strategy instance id.",
            detail="Refresh the panel and retry with the bot's own commands.",
        )
    try:
        return await _run_action_under_live_authority(
            broker, account_id, sid, request, operator_identity=operator_identity, reconciled=reconciled
        )
    except ExecutionLeaseLost as error:
        await _revive_lease_or_raise(broker, account_id, sid, request, error=error)
    except RepositoryPoisoned as error:
        _log_authority_unavailable(broker, account_id, sid, request, error, lost_lease=False)
        raise AuthorityPoisonedError() from error


async def _revive_lease_or_raise(
    broker: str,
    account_id: str,
    sid: str,
    request: PanelActionRequest,
    *,
    error: ExecutionLeaseLost,
) -> NoReturn:
    """ADR 0050 for the write path: revive, then tell the operator to retry --
    never retry the mutation here.

    Before this, every write-path ``ExecutionLeaseLost`` went straight to the
    terminal "restart the data plane" blocker (T7c/#1794) -- even the case
    ADR 0050 says self-cures: a process frozen past its lease TTL and thawed,
    with nobody else ever touching the account. See ADR 0050's 2026-09-06
    addendum for the full history of why this reports a typed, authored
    refusal for every outcome (success, refused, poisoned, transient error)
    instead of retrying the mutation in-process under a derived idempotency
    key (rounds 1 and 2; rejected by two independent reviews). Nothing this
    function raises implies the mutation was attempted twice, because it is
    never attempted a second time at all -- the operator's next click is a
    genuinely new request, admitted through the normal path with a fresh
    idempotency key (the panel client mints one per submission; see
    ``ExecutionAuthorityRevivedError``'s docstring).

    Revival itself delegates entirely to ``ReconciliationSweep.revive_now()``
    -- the same entry point the lease heartbeat's own tick uses
    (``_attempt_lease_revival``) -- rather than calling
    ``ClerkSqliteRepository.revive_execution_lease`` directly, so the CAS,
    its CRITICAL logging, and the ADR 0050 §3 post-revival recovery hook
    (``BotTaskRegistry.run_lease_recovery``) all run from the one
    implementation the heartbeat itself uses. There is no single-flight
    around ``revive_now()``: a concurrent heartbeat tick can call it on this
    same sweep instance at the same moment this write path does. See
    ``ReconciliationSweep.revive_now``'s own docstring for why that race is
    harmless rather than guarded against.

    The lease that lapsed must be *this* authority's. ``ExecutionLeaseLost``
    names the account whose lease it is; a Dry Run bot's lifecycle performers
    write through the binding's isolated ``sim:`` Clerk, so the exception can
    arrive here naming a synthetic authority while ``account_id`` is the real
    operator account the route was authorized against. That case is refused
    before the primary sweep is consulted, as the bot-scoped
    ``DryRunAuthorityLeaseLostError`` rather than the account-scoped loss (a
    cohort batch continues past it; the router reports ``conflict``): the
    synthetic authority revives through its own heartbeat, and the real
    account's recovery pass must not run on an unrelated bot's behalf.

    A synthetic authority is not "inherited" here in any sense -- it never
    reaches this function's ``sweep.revive_now()`` call at all.
    ``active_reconciliation_sweep`` and ``active_sqlite_facade`` share the
    same primary-authority selector, so a synthetic authority's
    ``facade`` lookup below already returns ``None`` and this raises
    ``REVIVAL_OUTCOME_AUTHORITY_UNAVAILABLE`` before any sweep is consulted.
    """
    facade = active_sqlite_facade(broker)
    if facade is None or not account_route_matches_custody(
        account_id, facade.account_id, shadow=facade.authority_kind == "shadow",
    ):
        # The active SQLite authority for this account is gone -- a restart
        # or reset raced this request, or it was never SQLite to begin with
        # (a synthetic authority's facade lookup also returns None here) --
        # so there is nothing left to revive.
        _log_authority_unavailable(broker, account_id, sid, request, error, lost_lease=True)
        raise ExecutionAuthorityLostError(
            revival_outcome=REVIVAL_OUTCOME_AUTHORITY_UNAVAILABLE, attempted=False
        ) from error

    if error.account_id is not None and error.account_id != facade.account_id:
        # The lease that lapsed is another authority's -- a Dry Run bot's
        # isolated ``sim:`` account, whose performers write through the
        # binding's synthetic Clerk while ``account_id`` here is still the
        # real operator account. The primary sweep is not this lease's, so
        # reviving it would mutate an unrelated authority and report
        # "revived" about a lease it never touched. Nothing is attempted, and
        # the error is bot-scoped: a cohort batch continues past this leg.
        _log_authority_unavailable(broker, account_id, sid, request, error, lost_lease=True)
        raise DryRunAuthorityLeaseLostError() from error

    sweep = active_reconciliation_sweep(broker)
    if sweep is None:
        # The authority is exactly the one this action was presented against,
        # but it has no reconciliation sweep to revive through (see
        # REVIVAL_OUTCOME_NO_SWEEP). Neither this nor the branch above ever
        # called revive_now(), so attempted=False -- the store never
        # "verified" anything here.
        _log_authority_unavailable(broker, account_id, sid, request, error, lost_lease=True)
        raise ExecutionAuthorityLostError(
            revival_outcome=REVIVAL_OUTCOME_NO_SWEEP, attempted=False
        ) from error

    try:
        await sweep.revive_now()
    except RepositoryPoisoned as poisoned:
        # Revival's own admission check (``_assert_not_poisoned``) found the
        # repository fenced by an unconfirmed mirror finalize -- a different
        # cure than a lost lease, so translate to the authority's own error.
        _log_authority_unavailable(broker, account_id, sid, request, poisoned, lost_lease=False)
        raise AuthorityPoisonedError() from poisoned
    except ExecutionLeaseLost as refusal:
        # Store-proven terminal loss: another writer or an authority
        # ceremony held the account. The ADR 0047 restart cure stands.
        _log_authority_unavailable(broker, account_id, sid, request, refusal, lost_lease=True)
        raise ExecutionAuthorityLostError(revival_outcome=REVIVAL_OUTCOME_REFUSED) from refusal
    except Exception as transient:
        # The CAS never reached a confirmed outcome (e.g. "database is
        # locked"). Unknown, not proven-lost -- report it as such rather
        # than the terminal restart copy, mirroring the heartbeat's own
        # "errored; retrying" vocabulary for the identical condition.
        _log_authority_unavailable(broker, account_id, sid, request, transient, lost_lease=True)
        raise ExecutionAuthorityLostError(
            revival_outcome=REVIVAL_OUTCOME_TRANSIENT_STORE_ERROR,
            remedy=REVIVAL_REMEDY_TRANSIENT_STORE_ERROR,
        ) from transient

    # Revival succeeded: the lease is good again, but this request's mutation
    # was never attempted under it. Report a retryable refusal instead of
    # retrying here (see this function's docstring for why).
    raise ExecutionAuthorityRevivedError() from error


def _log_authority_unavailable(
    broker: str,
    account_id: str,
    sid: str,
    request: PanelActionRequest,
    error: Exception,
    *,
    lost_lease: bool,
) -> None:
    """T7c (#1794): this account's authority cannot be written to. Refusing is
    correct; leaking the internal handle message as a raw 500 is not.
    Translated at the call sites above rather than in the router so SQLite
    repository internals stay behind this seam.

    The two conditions are kept apart because their cures differ: a lost
    lease means the ADR 0050 revival was refused (another writer or an
    authority ceremony holds the account) and a restart re-acquires it; a
    poisoned repository means this authority's own last transition is
    unproven until the fence reconciliation re-runs. Collapsing them would
    hand an operator the wrong remedy.
    """
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


async def _run_action_under_live_authority(
    broker: str,
    account_id: str,
    sid: str,
    request: PanelActionRequest,
    *,
    operator_identity: str,
    reconciled: ReconciliationCut | None,
) -> PanelActionResult:
    """Act in the one authority the bot's panel is read from.

    Reads always opened a Dry Run's own ``sim:`` Clerk while actions ran
    against the account's, which has never held that bot: Reconcile now
    answered "no custody record" and Flatten never unlocked (hurdle H33). The
    panel and the action now share one selection, held open across both, so
    a Dry Run recovers inside its simulator and never reaches Alpaca.
    """
    async with _selected_panel_authority(broker, account_id, sid) as (resolved, registry, binding, facade):
        panel, _entries, _session_fills = await _get_panel_with_entries_from_authority(
            broker, account_id, sid, resolved=resolved, captured_now_ms=now_ms_utc(),
            registry=registry, binding=binding, facade=facade,
        )
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
            # The refusal is the guard's own: its headline, its why and its
            # condition code, so a batch leg reports the reason its bot gave.
            blocker = action.blockers[0] if action.blockers else None
            availability_error = (
                ActionNotAvailableError(
                    f"The '{action.label}' action is blocked by the current panel state.",
                    detail="Refresh the panel and inspect the operation's readiness check.",
                )
                if blocker is None
                else ActionNotAvailableError(
                    blocker.headline, detail=blocker.detail, reason_code=blocker.condition.id
                )
            )
        try:
            sqlite_result = await execute_sqlite_panel_action(
                broker,
                account_id,
                sid,
                request=request,
                panel=panel,
                action=action,
                availability_error=availability_error,
                facade=facade,
                # Same durable receipt ledger the shared executor uses, so a
                # repost of an applied SQLite recovery action replays as a no-op
                # instead of re-executing (fleet run 2026-08-25 / F15).
                store=durable_idempotency_store_for(registry.artifacts_root, sid),
            )
        except SqlitePanelBotNotFound as exc:
            raise UnknownBotError(str(exc)) from exc
    if sqlite_result is not None:
        return sqlite_result
    return await execute_action(
        request,
        sid=sid,
        current_revision=panel.revision,
        current_concurrency_token=action.concurrency_token,
        performers=_action_performers(broker, sid, reconciled=reconciled),
        operator_identity=operator_identity,
        store=durable_idempotency_store_for(registry.artifacts_root, sid),
        availability_error=availability_error,
    )
