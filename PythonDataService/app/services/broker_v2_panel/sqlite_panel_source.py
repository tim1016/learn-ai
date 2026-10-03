"""SQLite authority integration seam for the existing Broker V2 panel."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import NoReturn

from app.broker.alpaca.clerk.account_authority import authority_kind_for_account
from app.broker.alpaca.clerk.models import ChannelHealth, ClerkStatus
from app.broker.alpaca.clerk.sqlite.decision_receipts import DecisionReceipt
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionError,
    EconomicSnapshot,
    FillWindowProjection,
    RunScope,
    SessionEconomicProjection,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.models import (
    ControlMetaSnapshot,
    DecisionReceiptResource,
)
from app.broker.alpaca.clerk.sqlite.projection_errors import ProjectionReadError
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionUnavailableError,
    StaleRecoveryTokenError,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    ExecutionLeaseLostAfterBrokerIO,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.lean_sidecar.trading_calendar import current_trading_session_window
from app.schemas.account_authority import AuthorityKind
from app.schemas.alpaca_clerk_sqlite import ExposureNoticeView
from app.schemas.bot_lifecycle import UNCLEAN_DUTY_OUTCOMES
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotGroup,
    BotPanelView,
    PanelAction,
    PanelActionRequest,
    PanelActionResult,
)
from app.schemas.decision_explanation import DecisionExplanationRecord
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    ActionOutcomeUnknownError,
    IdempotencyStore,
    StaleRevisionError,
    get_idempotency_store,
    outcome_unknown_after_broker_io,
)
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
    bot_group,
    bot_world,
    ended_at_ms,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    SQLITE_PANEL_LIFECYCLE_ACTION_IDS,
    CatalogHomeFacts,
    build_sqlite_catalog,
    ended_uncleanly,
    terminal_exposure_notices,
    with_finished_results,
)
from app.services.broker_v2_panel.sqlite_roster_status import (
    RosterMembership,
    build_roster_status,
    build_terminal_roster_status,
    roster_membership,
)
from app.services.sqlite_clerk_compat import (
    active_sqlite_facade,
    sqlite_clerk_status,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class SqlitePanelBotNotFound(ValueError):
    """The process registry has a bot absent from active SQLite custody."""

    @classmethod
    def for_bot(cls, strategy_instance_id: str) -> SqlitePanelBotNotFound:
        """The owner's words: the storage engine is not the problem, the missing record is."""
        return cls(f"No custody record exists for bot '{strategy_instance_id}'.")


# ── Coherence-retry policy ───────────────────────────────────────────────
# Every read here is all-or-unavailable: a torn cut is refused, never spliced.
# These bound how many times a seam re-samples before refusing.
_CATALOG_COHERENCE_ATTEMPTS = 2
_TORN_READ_ATTEMPTS = 3
_TORN_READ_BACKOFF_MS = 25


def _refuse_torn_read(
    what: str,
    message: str,
    *,
    broker: str,
    account_id: str,
    strategy_instance_id: str,
) -> NoReturn:
    """Refuse a projection that never settled, and record that it happened.

    Fail-closed either way -- a spliced cut is never served. The record is what
    distinguishes a load-correlated blip from custody churning faster than the
    projection can ever settle; without it both produce an identical 503
    (#1796).
    """
    logger.warning(
        "%s projection did not settle; refusing rather than splicing",
        what.capitalize(),
        extra={
            "action": f"sqlite_{what}_projection_torn_read_exhausted",
            "broker": broker,
            "account_id": account_id,
            "strategy_instance_id": strategy_instance_id,
            "attempts": _TORN_READ_ATTEMPTS,
        },
    )
    raise SqlitePanelEconomicUnavailable(message)


def _spaced_attempts(total: int = _TORN_READ_ATTEMPTS) -> Iterator[int]:
    """Yield attempt indices, spacing each retry so it samples a new moment.

    The attempts used to run back-to-back, so under a trading fleet's write
    burst they all sampled the same instant and all lost to the same burst --
    three retries spread over a few milliseconds are close to one (T5, #1796).
    The whole ladder stays far below the frontend's 15 s poll budget.

    The *count* is deliberately unchanged: picking a larger number is a guess,
    and the per-account contention behind these curves wants a profile first
    (#1801).

    Callers run inside ``asyncio.to_thread``, so sleeping here does not block
    the event loop.
    """
    for attempt in range(total):
        if attempt:
            time.sleep(_TORN_READ_BACKOFF_MS * attempt / 1000)
        yield attempt


class SqlitePanelEconomicUnavailable(RuntimeError):
    """SQLite cannot provide one coherent custody and economic panel cut."""


class SqlitePanelDecisionUnavailable(RuntimeError):
    """SQLite decision evidence cannot be rendered as the panel contract."""


@dataclass(frozen=True)
class SqlitePanelEvidence:
    """One revision-bound custody projection and S2 economic bundle."""

    status: BotStatusView
    projection: ClerkProjection
    economics: SessionEconomicProjection


@dataclass(frozen=True)
class SqliteChartEvidence:
    """Revision-bound SQLite bot identity and complete HISTORY fill window."""

    status: BotStatusView
    fills: FillWindowProjection


def read_sqlite_roster_statuses(broker: str) -> list[BotStatusView] | None:
    """Read the activated bot roster without consulting runner artifacts.

    SQLite activation makes ``strategy_instances`` and ``runs`` the durable
    roster/lifecycle authority.  The legacy binding tree is disposable process
    evidence and must not be walked by routine catalog refreshes.

    Immutable strategy identity comes from the S1 ``bot_config`` fold.  A
    missing config is an unavailable authority projection, never an
    ``unknown`` identity reconstructed from disposable runner artifacts.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    return [
        build_roster_status(broker, registration, facade.repository)
        for registration in facade.repository.strategy_instances()
    ]


def read_sqlite_bot_status(
    broker: str,
    strategy_instance_id: str,
) -> BotStatusView | None:
    """Read one activated bot's immutable identity and lifecycle from SQLite."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    registration = facade.repository.strategy_instance(strategy_instance_id)
    if registration is None:
        return None
    return build_roster_status(broker, registration, facade.repository)



def _meta_matches(
    meta: ControlMetaSnapshot,
    *,
    account_id: str,
    authority_generation: int,
    control_revision: int,
) -> bool:
    return (
        meta.account_id == account_id
        and meta.authority_generation == authority_generation
        and meta.control_revision == control_revision
    )


def _bound_bot_status(
    facade: object,
    broker: str,
    strategy_instance_id: str,
    *,
    account_id: str,
    authority_generation: int,
    control_revision: int,
) -> BotStatusView | None:
    """Read one lifecycle/config row between equal authority revision fences."""
    repository = facade.repository
    before = repository.control_meta_snapshot()
    if not _meta_matches(
        before,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite bot identity changed before lifecycle projection."
        )
    registration = repository.strategy_instance(strategy_instance_id)
    status = (
        build_roster_status(broker, registration, repository)
        if registration is not None
        else None
    )
    after = repository.control_meta_snapshot()
    if not _meta_matches(
        after,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite bot identity changed during lifecycle projection."
        )
    return status


def _bound_roster_statuses(
    facade: object,
    broker: str,
    *,
    account_id: str,
    authority_generation: int,
    control_revision: int,
    inert_terminal: frozenset[str],
) -> list[BotStatusView]:
    """Read roster identity/lifecycle rows between equal authority fences.

    ``inert_terminal`` is the classification the projection reads were chosen
    from. It is re-derived here, inside the fence, and any disagreement is a
    revision mismatch the caller retries: the membership read happens before
    the projection reads, so a retired bot that acquires live custody in
    between would otherwise keep its skip-the-projection classification while
    the economic fence accepted the newer revision -- surfacing the new
    exposure on a row with no custody projection, no ``needs_attention`` and
    no recovery command until a later poll.
    """
    repository = facade.repository
    before = repository.control_meta_snapshot()
    if not _meta_matches(
        before,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite roster identity changed before lifecycle projection."
        )
    if roster_membership(repository).inert_terminal != inert_terminal:
        raise SqliteCatalogRevisionMismatch(
            "SQLite roster custody changed after the catalog chose its projections."
        )
    statuses = [
        build_terminal_roster_status(broker, registration, repository)
        if str(registration["strategy_instance_id"]) in inert_terminal
        else build_roster_status(broker, registration, repository)
        for registration in repository.strategy_instances()
    ]
    after = repository.control_meta_snapshot()
    if not _meta_matches(
        after,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite roster identity changed during lifecycle projection."
        )
    return statuses


async def read_sqlite_clerk_status(
    broker: str = "alpaca",
    *,
    symbol: str | None = None,
    facade: SqliteAlpacaClerkFacade | None = None,
) -> ClerkStatus | None:
    """One authority's Clerk card facts: the account's, or a Dry Run's own ``sim:`` Clerk.

    A Dry Run's card names its simulated account, never the real one it is
    listed under (hurdle H33).
    """
    facade = facade or active_sqlite_facade(broker)
    if facade is None:
        return None
    projection = await asyncio.to_thread(_account_projection, facade)
    return sqlite_clerk_status(
        projection,
        last_clean_pass_at_ms=facade.last_clean_pass_at_ms(),
        channel_healths=_channel_healths(broker, facade, symbol),
    )


def _account_projection(facade: SqliteAlpacaClerkFacade) -> ClerkProjection:
    reader = SqliteClerkProjectionReader.from_facade(facade)
    try:
        return reader.account_snapshot()
    finally:
        reader.close()


def _channel_healths(
    broker: str,
    facade: SqliteAlpacaClerkFacade,
    symbol: str | None,
) -> tuple[ChannelHealth, ...] | None:
    """The submission channels an authority depends on.

    A Dry Run's simulator gates no channel of its own. It decides on the
    process's IBKR market data and never opens Alpaca's execution channel, so
    its card carries that one shared channel and nothing about Alpaca.
    """
    own = facade.channel_health_snapshot(symbol)
    if own is not None or facade.authority_kind != "synthetic":
        return own
    account = active_sqlite_facade(broker)
    shared = None if account is None else account.channel_health_snapshot(symbol)
    if shared is None:
        return None
    return tuple(health for health in shared if health.stream == "market_data")


async def read_sqlite_panel_evidence(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    now_ms: int,
    facade: SqliteAlpacaClerkFacade | None = None,
    run: RunScope | None = None,
) -> SqlitePanelEvidence | None:
    """Read one panel's custody and S2 economics at the same SQLite revision.

    Separate reader classes use their own read transactions. A writer can
    commit between them, so this seam retries a bounded number of times and
    accepts only identical account, generation, and control-revision values.
    It never fills a mismatch from JSONL or a broker endpoint.

    ``None`` means no SQLite authority is active for ``broker`` — the
    authority itself is missing, so callers answer 503. A bot that the
    active authority simply does not carry raises
    ``SqlitePanelBotNotFound``, which callers answer 404. Collapsing the two
    tells an operator to repair a healthy authority.

    ``run`` also reads that run's fills and activity, at the same revision:
    the bot page summarizes its run and lists its fills (#2794).
    """
    facade = facade or active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    session_window = current_trading_session_window(now_ms)

    def read_once() -> SqlitePanelEvidence | None:
        for _attempt in _spaced_attempts():
            custody_reader = SqliteClerkProjectionReader.from_facade(facade)
            economic_reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
            try:
                projection = custody_reader.bot_snapshot(strategy_instance_id)
                economics = economic_reader.bot_session_economic_projection(
                    strategy_instance_id,
                    session_window=session_window,
                    run=run,
                )
            finally:
                custody_reader.close()
                economic_reader.close()
            if projection is None or economics is None:
                raise SqlitePanelBotNotFound.for_bot(strategy_instance_id)
            snapshot = economics.snapshot
            if (
                projection.account_id == snapshot.account_id
                and projection.authority_generation == snapshot.authority_generation
                and projection.control_revision == snapshot.control_revision
                and projection.strategy_instance_id == snapshot.strategy_instance_id
            ):
                try:
                    status = _bound_bot_status(
                        facade,
                        broker,
                        strategy_instance_id,
                        account_id=snapshot.account_id,
                        authority_generation=snapshot.authority_generation,
                        control_revision=snapshot.control_revision,
                    )
                except SqliteCatalogRevisionMismatch:
                    continue
                if status is None:
                    raise SqlitePanelBotNotFound.for_bot(strategy_instance_id)
                return SqlitePanelEvidence(
                    status=status,
                    projection=projection,
                    economics=economics,
                )
        _refuse_torn_read(
            "panel",
            "SQLite custody and execution economics changed during panel projection.",
            broker=broker,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
        )

    try:
        return await asyncio.to_thread(read_once)
    except (EconomicProjectionError, SqliteCatalogProjectionUnavailable) as exc:
        raise SqlitePanelEconomicUnavailable(str(exc)) from exc


async def read_sqlite_chart_fills(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    from_ms: int,
    to_ms: int,
) -> FillWindowProjection | None:
    """Read every effective SQLite fill for one bounded HISTORY chart window.

    The S2 reader returns all-or-unavailable, rather than a truncated tail, so
    chart markers remain a complete execution account for their window.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")

    def read_window() -> FillWindowProjection | None:
        reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
        try:
            return reader.bot_fill_window(
                strategy_instance_id,
                from_ms=from_ms,
                to_ms=to_ms,
            )
        finally:
            reader.close()

    try:
        return await asyncio.to_thread(read_window)
    except (EconomicProjectionError, SqliteCatalogProjectionUnavailable) as exc:
        raise SqlitePanelEconomicUnavailable(str(exc)) from exc


async def read_sqlite_chart_evidence(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    from_ms: int,
    to_ms: int,
) -> SqliteChartEvidence | None:
    """Read one HISTORY marker window and its identity at one revision fence."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")

    def read_once() -> SqliteChartEvidence | None:
        for _attempt in _spaced_attempts():
            reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
            try:
                fills = reader.bot_fill_window(
                    strategy_instance_id,
                    from_ms=from_ms,
                    to_ms=to_ms,
                )
            finally:
                reader.close()
            if fills is None:
                return None
            try:
                status = _bound_bot_status(
                    facade,
                    broker,
                    strategy_instance_id,
                    account_id=fills.account_id,
                    authority_generation=fills.authority_generation,
                    control_revision=fills.control_revision,
                )
            except SqliteCatalogRevisionMismatch:
                continue
            if status is None:
                return None
            return SqliteChartEvidence(status=status, fills=fills)
        _refuse_torn_read(
            "chart",
            "SQLite history identity and execution folds changed during projection.",
            broker=broker,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
        )

    try:
        return await asyncio.to_thread(read_once)
    except (EconomicProjectionError, SqliteCatalogProjectionUnavailable) as exc:
        raise SqlitePanelEconomicUnavailable(str(exc)) from exc


def read_sqlite_decision_receipts(
    broker: str,
    strategy_instance_id: str,
    *,
    limit: int = 8,
    facade: SqliteAlpacaClerkFacade | None = None,
) -> list[DecisionReceipt] | None:
    """Read bounded decision evidence, with its stored causal links, from SQLite.

    Never the legacy JSONL tail.
    """
    facade = facade or active_sqlite_facade(broker)
    if facade is None:
        return None
    try:
        resources = facade.repository.decision_receipt_tail(
            strategy_instance_id=strategy_instance_id,
            limit=limit,
        )
    except (TypeError, ValueError) as exc:
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision evidence is unavailable for bot '{strategy_instance_id}'."
        ) from exc
    # Named `adapted_views`, not `receipts` — the AST writer-boundary scan
    # (`test_repository_writer_boundary.py`) treats any `receipts.append(...)`
    # call as a durable `SqliteDecisionReceipts.append` mutation; this is a
    # plain in-memory list of already-read display DTOs, not a repository
    # handle.
    adapted_views: list[DecisionReceipt] = [
        _decision_receipt_from_resource(resource) for resource in resources
    ]
    return adapted_views


def _decision_receipt_from_resource(
    resource: DecisionReceiptResource,
) -> DecisionReceipt:
    """Adapt a durable S1 receipt to the panel evidence view, causal links attached."""
    try:
        facts = json.loads(resource.facts_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision receipt {resource.seq} has invalid facts JSON."
        ) from exc
    if not isinstance(facts, dict):
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision receipt {resource.seq} facts must be an object."
        )
    explanation = _facts_explanation(resource.seq, facts)
    try:
        # `decision_id` == `evaluation_id` (ADR 0043 decision 5), and the atomic
        # writer stamps both keys to that one value, so reading either is
        # equivalent rather than a choice between two different facts. The
        # second read is a compatibility path for rows persisted before both
        # keys were written; it is deliberately not a precedence rule.
        return DecisionReceipt.model_validate(
            {
                "seq": resource.seq,
                "ts_ms": resource.observed_at_ms,
                "bar_ref": facts["bar_ref"],
                "outcome": resource.outcome,
                "reason_code": facts["reason_code"],
                "intent_id": resource.intent_id or "",
                "order_ref": resource.order_ref or "",
                "run_id": _facts_optional_str(facts, "run_id"),
                "decision_bar_close_ms": facts.get("decision_bar_close_ms"),
                "explanation": explanation,
                "explanation_unreadable": explanation is None and facts.get("explanation") is not None,
                "decision_id": (
                    _facts_optional_str(facts, "decision_id")
                    or _facts_optional_str(facts, "evaluation_id")
                ),
                "effect_operation_id": _facts_optional_str(facts, "effect_operation_id"),
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision receipt {resource.seq} does not satisfy the panel evidence contract."
        ) from exc


def _facts_explanation(seq: int, facts: dict) -> DecisionExplanationRecord | None:
    """Read a receipt's decision explanation (#2639); ``None`` when it has none.

    The explanation is display evidence, never a custody fact, so one this
    build cannot read (a row from a newer schema) is reported and left out
    rather than taking the whole decision record down with it.
    """
    raw = facts.get("explanation")
    if raw is None:
        return None
    try:
        return DecisionExplanationRecord.model_validate(raw)
    except ValueError as exc:
        logger.warning(
            "A decision receipt's explanation could not be read",
            extra={"action": "decision_explanation_unreadable", "receipt_seq": seq, "reason": str(exc)},
        )
        return None


def _facts_optional_str(facts: dict, key: str) -> str | None:
    """Read one optional string identity out of decision-receipt facts.

    A present-but-empty or non-string value is treated the same as absent
    (``None``) rather than raising — these are supplementary causal-link
    identities, not the receipt's own required contract fields.
    """
    value = facts.get(key)
    return value if isinstance(value, str) and value else None


async def read_sqlite_catalog_projections(
    broker: str,
    account_id: str,
    strategy_instance_ids: list[str],
) -> dict[str, ClerkProjection] | None:
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")

    def read_all() -> dict[str, ClerkProjection]:
        reader = SqliteClerkProjectionReader.from_facade(facade)
        try:
            return {
                strategy_instance_id: projection
                for strategy_instance_id in strategy_instance_ids
                if (
                    projection := reader.bot_snapshot(strategy_instance_id)
                ) is not None
            }
        finally:
            reader.close()

    return await asyncio.to_thread(read_all)


async def read_sqlite_catalog_economic_rollups(
    broker: str,
    account_id: str,
    strategy_instance_ids: list[str],
) -> dict[str, EconomicSnapshot] | None:
    """Read all requested roster economics in one S2 SQLite revision.

    The S2 reader holds a single SQLite read transaction across every requested
    bot.  The active product path therefore never mixes fill/P&L values from
    JSONL, a process registry, or independently timed per-bot reads.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    session_window = current_trading_session_window(now_ms_utc())

    def read_all() -> dict[str, EconomicSnapshot]:
        reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
        try:
            return reader.catalog_economic_rollup(
                strategy_instance_ids,
                session_window=session_window,
            )
        finally:
            reader.close()

    try:
        return await asyncio.to_thread(read_all)
    except EconomicProjectionError as exc:
        raise SqliteCatalogProjectionUnavailable(str(exc)) from exc


async def read_sqlite_catalog(
    broker: str,
    account_id: str,
) -> list[BotCatalogView] | None:
    """Build the activated roster from SQLite-only lifecycle and economic folds."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    for attempt in range(_CATALOG_COHERENCE_ATTEMPTS):
        # Identities only. Building full status rows here and keeping just
        # their ids would read every bot's lifecycle file twice per request
        # (again per coherence retry); the fenced pass below builds each row
        # exactly once, at a revision it can vouch for.
        membership = await asyncio.to_thread(roster_membership, facade.repository)
        # A cleared bot -- retired, nothing live in custody -- is not on Home
        # (#2567): the catalog reads and returns only the rest (#1911).
        strategy_instance_ids = membership.with_live_custody
        if not strategy_instance_ids:
            return []
        projections = await read_sqlite_catalog_projections(
            broker,
            account_id,
            strategy_instance_ids,
        )
        economic_rollups = await read_sqlite_catalog_economic_rollups(
            broker,
            account_id,
            strategy_instance_ids,
        )
        if projections is None or economic_rollups is None:
            raise SqliteCatalogProjectionUnavailable(
                "The active SQLite authority became unavailable during catalog projection."
            )
        try:
            first_snapshot = next(iter(economic_rollups.values()), None)
            if first_snapshot is None:
                raise SqliteCatalogRevisionMismatch(
                    "SQLite catalog has no economic revision to bind lifecycle identity."
                )
            rows = await asyncio.to_thread(
                _bind_catalog_rows,
                facade,
                broker,
                account_id=account_id,
                membership=membership,
                projections=projections,
                economic_rollups=economic_rollups,
                fence=first_snapshot,
            )
        except SqliteCatalogRevisionMismatch:
            if attempt == _CATALOG_COHERENCE_ATTEMPTS - 1:
                raise
            continue
        return await with_finished_results(rows, facade.repository.bot_results)
    raise AssertionError("catalog coherence retry exhausted without a result")


def _bind_catalog_rows(
    facade: SqliteAlpacaClerkFacade,
    broker: str,
    *,
    account_id: str,
    membership: RosterMembership,
    projections: dict[str, ClerkProjection],
    economic_rollups: dict[str, EconomicSnapshot],
    fence: EconomicSnapshot,
) -> list[BotCatalogView]:
    """Bind one projection cut to the roster's lifecycle rows and Home facts.

    Raises ``SqliteCatalogRevisionMismatch`` when the roster moved under the
    cut, for the caller's coherence retry. Blocking: every row's lifecycle,
    the holding set's fill-lineage claims scan and the latest run stops are
    SQLite reads under the write lock, so callers run it off the event loop
    (a Home tab polls the catalog every 5 s).
    """
    statuses = _bound_roster_statuses(
        facade,
        broker,
        account_id=fence.account_id,
        authority_generation=fence.authority_generation,
        control_revision=fence.control_revision,
        inert_terminal=membership.inert_terminal,
    )
    if [status.strategy_instance_id for status in statuses] != membership.identities:
        raise SqliteCatalogRevisionMismatch(
            "SQLite roster membership changed during catalog projection."
        )
    # A cleared bot leaves the catalog (#2567); its records stay, readable by id.
    return build_sqlite_catalog(
        [status for status in statuses if status.strategy_instance_id not in membership.inert_terminal],
        projections,
        economic_rollups=economic_rollups,
        account_id=account_id,
        home=_catalog_home(facade),
    )


def _catalog_home(facade: SqliteAlpacaClerkFacade) -> CatalogHomeFacts:
    """What places one authority's roster rows on its account's Home. Blocking."""
    repository = facade.repository
    return CatalogHomeFacts(
        world=authority_kind_for_account(facade.account_id, account_mode=facade.account_mode),
        holding_money=repository.bots_holding_money(),
        latest_stops=repository.latest_run_stops(),
    )


async def read_sqlite_catalog_from_facade(
    broker: str,
    facade: SqliteAlpacaClerkFacade,
) -> list[BotCatalogView]:
    """Project one explicitly selected account authority into the V2 roster.

    The default catalog entry point deliberately resolves only the real-paper
    compatibility authority. Dry Run callers already possess an account-keyed
    synthetic Clerk, so routing them through that global selector would make a
    successful sealed run disappear from the roster.
    """
    account_id = facade.account_id
    for attempt in range(_CATALOG_COHERENCE_ATTEMPTS):
        membership = await asyncio.to_thread(roster_membership, facade.repository)
        # A cleared bot is not on Home (#2567); see ``read_sqlite_catalog``.
        strategy_instance_ids = membership.with_live_custody
        if not strategy_instance_ids:
            return []

        def read_projection_cut(
            selected_ids: tuple[str, ...] = tuple(strategy_instance_ids),
            projected_ids: tuple[str, ...] = tuple(strategy_instance_ids),
        ) -> tuple[dict[str, ClerkProjection], dict[str, EconomicSnapshot]]:
            custody_reader = SqliteClerkProjectionReader.from_facade(facade)
            economic_reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
            try:
                # Custody for the rows that can still need attention; economics
                # for every row, since that read is one batched query (#1911).
                projections = {
                    strategy_instance_id: projection
                    for strategy_instance_id in projected_ids
                    if (
                        projection := custody_reader.bot_snapshot(strategy_instance_id)
                    ) is not None
                }
                economics = economic_reader.catalog_economic_rollup(
                    selected_ids,
                    session_window=current_trading_session_window(now_ms_utc()),
                )
                return projections, economics
            finally:
                custody_reader.close()
                economic_reader.close()

        try:
            projections, economic_rollups = await asyncio.to_thread(read_projection_cut)
        except EconomicProjectionError as exc:
            raise SqliteCatalogProjectionUnavailable(str(exc)) from exc
        first_snapshot = next(iter(economic_rollups.values()), None)
        if first_snapshot is None:
            raise SqliteCatalogRevisionMismatch(
                "SQLite catalog has no economic revision to bind lifecycle identity."
            )
        try:
            rows = await asyncio.to_thread(
                _bind_catalog_rows,
                facade,
                broker,
                account_id=account_id,
                membership=membership,
                projections=projections,
                economic_rollups=economic_rollups,
                fence=first_snapshot,
            )
        except SqliteCatalogRevisionMismatch:
            if attempt == _CATALOG_COHERENCE_ATTEMPTS - 1:
                raise
            continue
        return await with_finished_results(rows, facade.repository.bot_results)
    raise AssertionError("catalog coherence retry exhausted without a result")


async def execute_sqlite_panel_action(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    request: PanelActionRequest,
    panel: BotPanelView,
    action: PanelAction,
    availability_error: ActionNotAvailableError | None,
    facade: SqliteAlpacaClerkFacade | None,
    store: IdempotencyStore | None = None,
) -> PanelActionResult | None:
    """Execute through the same policy that authored the presented action.

    ``facade`` is the authority the bot's panel was read from
    (``bot_custody``'s one selection): a Dry Run's own
    ``sim:`` Clerk, whose ports are its simulator, or the account's. Acting
    anywhere else is how a Dry Run's recovery used to ask the real account
    about a bot it has never held (hurdle H33). ``None`` means no SQLite
    authority is selected, and the caller's legacy performers apply.
    """
    if facade is None:
        return None
    if request.action_id in SQLITE_PANEL_LIFECYCLE_ACTION_IDS:
        return None

    # Idempotency BEFORE the staleness fence, mirroring the shared executor
    # (action_execution_service.execute_action): a genuine retry of an
    # already-applied action stays a safe no-op even after the panel advances.
    # The SQLite path previously had no idempotency ledger at all — the old
    # revision fence accidentally doubled as its double-execution guard.
    ledger = store if store is not None else get_idempotency_store()
    record = await ledger.reserve_or_get(
        strategy_instance_id, request.action_id, request.idempotency_key
    )
    if record is not None:
        if record.state == "succeeded" and record.result is not None:
            return PanelActionResult(
                action_id=request.action_id,
                receipt_id=record.result.receipt_id,
                recorded_at_ms=record.result.recorded_at_ms,
                applied=False,
                revision=panel.revision,
                concurrency_token=action.concurrency_token,
                message=record.result.message,
            )
        if record.state == "failed":
            raise ActionNotAvailableError(
                "This action previously failed; the idempotency key cannot be reused.",
                detail=record.error_detail,
            )
        raise ActionOutcomeUnknownError(
            "This command is still processing.",
            detail=(
                "No terminal receipt arrived before the idempotency wait expired. "
                "Do not retry this key; inspect Clerk evidence for the final outcome."
            ),
        )

    async def _release_reservation() -> None:
        await ledger.release(
            strategy_instance_id, request.action_id, request.idempotency_key
        )

    try:
        # Optimistic-concurrency fence. The action-scoped concurrency_token is
        # the authoritative staleness check per the PanelAction contract
        # (schemas/broker_v2_panel.py): it is deliberately narrower than the
        # display revision so unrelated churn cannot make a presented action
        # falsely stale. Fencing on strict revision equality is unsatisfiable —
        # every panel read (including this executor's own re-derivation) bumps
        # the revision, so panel.revision is always ahead of request.revision
        # (fleet run 2026-08-25: 15/15 action POSTs 409'd on an idle account).
        # The recovery layer re-proves its own token at commit
        # (StaleRecoveryTokenError below): fence for admission, proof for
        # commitment.
        if request.concurrency_token != action.concurrency_token:
            raise StaleRevisionError(
                "This bot's custody changed after this action was presented.",
                detail="Refresh the panel and review the current evidence-bound action.",
            )
        if availability_error is not None:
            raise availability_error
        if request.action_id in {"open_custody_timeline", "prepare_safe_flatten"}:
            raise ActionNotAvailableError(
                "This recovery capability is a view action, not a broker mutation.",
                detail="Use the presented navigation control in the panel.",
            )
    except (StaleRevisionError, ActionNotAvailableError):
        # Pre-execution rejection: nothing ran, free the key for a corrected
        # retry (same contract as the shared executor's release-on-reject).
        await _release_reservation()
        raise

    async def current_context():
        def read_context():
            reader = SqliteClerkProjectionReader.from_facade(facade)
            try:
                return reader.recovery_context(
                    strategy_instance_id=strategy_instance_id
                )
            finally:
                reader.close()

        context = await asyncio.to_thread(read_context)
        if context is None:
            raise SqlitePanelBotNotFound.for_bot(strategy_instance_id)
        return context

    try:
        context = await current_context()
    except Exception:
        # Context/catalog projection is still pre-execution. A transient read
        # failure must not strand the key as an outcome-unknown command when
        # no mutation was attempted.
        await _release_reservation()
        raise
    capability = next(
        (
            candidate
            for candidate in build_recovery_catalog(context)
            if candidate.action_id == request.action_id
        ),
        None,
    )
    try:
        result = await execute_recovery_action(
            facade,
            request=RecoveryExecutionRequest(
                action_id=request.action_id,
                concurrency_token=request.concurrency_token,
                execution_ref=(
                    capability.execution_ref if capability is not None else None
                ),
                reason=request.reason,
            ),
            current_context=current_context,
        )
    except StaleRecoveryTokenError as exc:
        # Recovery-layer admission rejection: nothing was committed.
        await _release_reservation()
        raise StaleRevisionError(
            str(exc),
            detail="Refresh the panel before retrying.",
        ) from exc
    except RecoveryActionUnavailableError as exc:
        await _release_reservation()
        raise ActionNotAvailableError(
            str(exc),
            detail=exc.capability.next_step,
        ) from exc
    except ExecutionLeaseLostAfterBrokerIO as exc:
        # The broker already acted (ClaimedBrokerIO's post-I/O renewal):
        # outcome unknown, key burned, no retry -- the release branch below
        # is only honest for a lease lost before any mutation.
        await ledger.fail(
            strategy_instance_id, request.action_id, request.idempotency_key, str(exc)
        )
        raise outcome_unknown_after_broker_io(exc) from exc
    except Exception as exc:
        if request.action_id == "stop_bot_decisions" or isinstance(exc, ExecutionLeaseLost):
            # STOP is durably idempotent beneath this panel ledger, so a Stop
            # that failed before its STOP committed is retried whole under
            # the same key. One that failed after -- its local task
            # quiescence, say -- is not redone from here: with no run left
            # ACTIVE the presented Stop is stale and a fresh one unavailable
            # (NO_ACTIVE_BOT_RUN, no execution_ref), so the retry never
            # reaches the recovery layer's existing-command branch. A raw
            # ``runs/stop``, or the Clerk's recovery-actions route naming the
            # run, replays that STOP and re-drives the quiescence (#2664).
            #
            # A lost execution lease releases for every action, not only
            # stop_bot_decisions: every repository mutation renews the lease
            # as its very first statement under the write lock
            # (repository.py), so the mutation that raised applied nothing.
            # Earlier legs of a multi-leg action may have applied
            # (safe_flatten submits leg 1 before leg 2 renews); the same-key
            # re-POST is still safe because execute_safe_flatten_plan
            # refuses a symbol an EXIT already owns
            # (exit.newest_reducible_entry). panel_data_source
            # .run_action's ADR 0050 revival needs this ORIGINAL idempotency
            # key free for the operator's (or a cohort batch's same-key)
            # re-POST -- a ``failed`` burn here left a revived leg
            # permanently unflattenable under its own key (#1955 final review).
            await _release_reservation()
        else:
            # Other attempted actions/errors have no equivalent committed-command
            # replay contract, so a blind same-key retry remains unsafe.
            await ledger.fail(
                strategy_instance_id, request.action_id, request.idempotency_key, str(exc)
            )
        if isinstance(exc, RecoveryExecutionError):
            raise ActionNotAvailableError(str(exc)) from exc
        raise

    outcome = PanelActionResult(
        action_id=request.action_id,
        receipt_id=result.receipt_id,
        recorded_at_ms=result.recorded_at_ms,
        applied=result.applied,
        revision=panel.revision,
        concurrency_token=action.concurrency_token,
        message=_outcome_message(
            action,
            applied=result.applied,
            simulated=facade.authority_kind == "synthetic",
        ),
    )
    await ledger.complete(
        strategy_instance_id, request.action_id, request.idempotency_key, outcome
    )
    return outcome


# A Dry Run's recovery happens inside its own simulation, and its receipt says
# so: a reconciliation there compares the Clerk with the simulator's own
# records, and a flatten is a simulated sale. Neither ever reaches Alpaca.
_SIMULATED_OUTCOMES = {
    "reconcile_now": (
        "Reconciled against this Dry Run's own simulated records, the only truth a "
        "simulation has; nothing was checked with Alpaca."
    ),
    "execute_safe_flatten": (
        "Simulated sale recorded at IBKR's live price when it went out; "
        "nothing was sent to Alpaca."
    ),
}


def _outcome_message(action: PanelAction, *, applied: bool, simulated: bool) -> str:
    if not applied:
        return f"{action.label} was already durably recorded."
    if simulated:
        return _SIMULATED_OUTCOMES.get(
            action.action_id,
            f"{action.label} completed in this Dry Run's simulation; nothing was sent to Alpaca.",
        )
    return f"{action.label} completed."


__all__ = [
    "SqliteChartEvidence",
    "SqlitePanelBotNotFound",
    "SqlitePanelDecisionUnavailable",
    "SqlitePanelEconomicUnavailable",
    "execute_sqlite_panel_action",
    "read_sqlite_bot_status",
    "read_sqlite_catalog",
    "read_sqlite_catalog_economic_rollups",
    "read_sqlite_catalog_from_facade",
    "read_sqlite_catalog_projections",
    "read_sqlite_chart_evidence",
    "read_sqlite_chart_fills",
    "read_sqlite_clerk_status",
    "read_sqlite_decision_receipts",
    "read_sqlite_panel_evidence",
    "read_sqlite_roster_statuses",
]


async def read_account_projection(facade: SqliteAlpacaClerkFacade) -> ClerkProjection:
    """The account's custody projection alone, on its own snapshot, off the loop."""
    def read() -> ClerkProjection:
        reader = SqliteClerkProjectionReader.from_facade(facade)
        try:
            return reader.account_snapshot()
        finally:
            reader.close()

    return await asyncio.to_thread(read)


@dataclass(frozen=True)
class HomeRosterBot:
    """Where one live registration sits on its account's Home (PRD #2560 D7).

    ``group`` is ``None`` when the bot's lifecycle cannot be read, so where it
    sits is unknown. ``unclean_ended_at_ms`` is when a stopped bot's run ended
    without a clean exit (a crash, an unverified exit), else ``None``.
    """

    strategy_instance_id: str
    symbol: str
    group: BotGroup | None
    unclean_ended_at_ms: int | None


def home_roster(repository: ClerkSqliteRepository, *, world: AuthorityKind) -> list[HomeRosterBot]:
    """Each live registration's Home group, placed by the catalog's own rule.

    The same roster status the catalog builds -- so a crashed bot whose run
    row is stuck ACTIVE is stopped here exactly as it is there -- the same
    holding set (``bots_holding_money``) and the same ``bot_group``. A retired
    registration with no live custody is the catalog's inert row (#1911) and
    is skipped. A bot whose lifecycle cannot be read is kept, ungrouped: a
    bad bot never hides its siblings. Blocking: callers run it off the loop.
    """
    membership = roster_membership(repository)
    holding = repository.bots_holding_money()
    stops = repository.latest_run_stops()
    bots: list[HomeRosterBot] = []
    for registration in repository.strategy_instances():
        sid = str(registration["strategy_instance_id"])
        if sid in membership.inert_terminal:
            continue
        try:
            status = build_roster_status("alpaca", registration, repository)
        except SqliteCatalogProjectionUnavailable:
            logger.error("Could not read one bot's lifecycle for Home", extra={
                "action": "home_roster_unreadable", "strategy_instance_id": sid,
                "account_id": repository.account_id,
            }, exc_info=True)
            bots.append(HomeRosterBot(sid, str(registration["symbol"]), None, None))
            continue
        outcome = status.duty_outcome
        bot_world_kind = bot_world(world, status.mode)
        # A Dry Run's unclean end is its simulator's to recover, never the
        # account's "Reconcile now" line, even once it is Finished (#2567).
        unclean = (
            bot_world_kind != "synthetic" and not status.running
            and outcome is not None and outcome.kind in UNCLEAN_DUTY_OUTCOMES
        )
        bots.append(HomeRosterBot(
            strategy_instance_id=sid,
            symbol=status.symbol,
            group=bot_group(world=bot_world_kind, running=status.running, holds_money=sid in holding),
            unclean_ended_at_ms=ended_at_ms(status, latest_stop_ms=stops.get(sid)) if unclean else None,
        ))
    return bots


@dataclass(frozen=True)
class _UncleanEnd:
    """One uncleanly ended bot, with its custody cut on the account's snapshot.

    ``projection`` is ``None`` when its lifecycle or custody evidence could not be read.
    """

    sid: str
    symbol: str
    kind: str = ""
    projection: ClerkProjection | None = None


async def read_account_custody(
    facade: SqliteAlpacaClerkFacade,
) -> tuple[ClerkProjection, list[ExposureNoticeView]]:
    """Read desk and bell custody with terminal notices on one SQLite snapshot."""
    def read() -> tuple[ClerkProjection, list[_UncleanEnd]]:
        reader = SqliteClerkProjectionReader.from_facade(facade)
        try:
            with reader.snapshot():
                projection = reader.account_snapshot()
                return projection, _unclean_ends(facade, reader)
        finally:
            reader.close()

    projection, ends = await asyncio.to_thread(read)
    return projection, await _terminal_exposure_notices(facade, ends)


def _unclean_ends(
    facade: SqliteAlpacaClerkFacade, reader: SqliteClerkProjectionReader,
) -> list[_UncleanEnd]:
    """Reuse roster lifecycle truth; a bad bot never hides its healthy siblings. Blocking."""
    ends: list[_UncleanEnd] = []
    repository = facade.repository
    # A retired registration with no live custody is the catalog's inert row
    # (#1911): retirement settled its outcome, and nothing of it can need the
    # bell. Skipping it keeps this walk from growing with retired history.
    inert = roster_membership(repository).inert_terminal
    for registration in repository.strategy_instances():
        sid = str(registration["strategy_instance_id"])
        if sid in inert:
            continue
        try:
            status = build_roster_status("alpaca", registration, repository)
            outcome = status.duty_outcome
            if outcome is None or not ended_uncleanly(kind=outcome.kind, running=status.running):
                continue
            projection = reader.bot_snapshot(sid)
            if projection is None:
                raise ProjectionReadError(f"Custody projection is missing for {sid}")
            ends.append(_UncleanEnd(sid=sid, symbol=status.symbol, kind=outcome.kind, projection=projection))
        except (SqliteCatalogProjectionUnavailable, ProjectionReadError):
            logger.error("Could not read one bot's terminal custody evidence", extra={
                "action": "terminal_exposure_unreadable", "strategy_instance_id": sid,
                "account_id": repository.account_id,
            }, exc_info=True)
            ends.append(_UncleanEnd(sid=sid, symbol=str(registration["symbol"])))
    return ends


async def _terminal_exposure_notices(
    facade: SqliteAlpacaClerkFacade, ends: list[_UncleanEnd],
) -> list[ExposureNoticeView]:
    """Author each ended bot's notices, accepting the Clerk's latest pass as its check (#2826).

    On the event loop: ``published_custody`` reads under the intake lock.
    """
    notices: list[ExposureNoticeView] = []
    for end in ends:
        if end.projection is None:
            notices.append(ExposureNoticeView(
                strategy_instance_id=end.sid, symbol=end.symbol,
                kind="position_unverified", label="Position could not be verified",
                # Every fix it names is in the app (hurdle H29).
                explanation=(
                    "This bot's lifecycle or custody evidence could not be read, so the app cannot vouch "
                    "for what it holds. Reconcile now re-reads the account at Alpaca; Flatten becomes "
                    "available once the position is proven."
                ),
                action_label="Open bot",
            ))
            continue
        notices.extend(terminal_exposure_notices(
            end.projection, sid=end.sid, symbol=end.symbol, kind=end.kind, running=False,
            pass_proof=await facade.published_custody(end.sid),
        ))
    return notices
