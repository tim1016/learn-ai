"""IBKR Account Truth projection.

This module is the account-wide counterpart to the per-bot broker-activity
publisher: it joins current broker facts with known order_ref namespaces and
authors operator-facing verdicts for Account Monitor, Reconciliation, and
Orders.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from pydantic import ValidationError

from app.broker.ibkr import account as ibkr_account
from app.broker.ibkr.account_recovery import (
    AccountRecoveryState,
    read_account_recovery_state,
)
from app.broker.ibkr.account_truth_freshness import (
    compose_account_truth_source_freshness,
    critical_source_freshness_blocks,
)
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrOpenOrder,
    IbkrOrderEvent,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.broker.ibkr.order_cancel_capability import evaluate_order_cancel_capability
from app.broker.ibkr.order_history import list_completed_orders
from app.broker.ibkr.orders import executions_for_reconnect_recovery, list_open_orders
from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    index_account_instance_bindings,
    read_account_instance_registry,
)
from app.engine.live.order_identity import (
    MANUAL_NAMESPACE_ROOT,
    NAMESPACE_ROOT,
    NAMESPACE_SEP,
    NAMESPACE_VERSION,
    parse_order_ref,
)
from app.schemas.account_truth import (
    AccountTruthEvidenceGap,
    AccountTruthExecutionRow,
    AccountTruthExecutionUncertaintyCode,
    AccountTruthFactOwner,
    AccountTruthFinalVerdict,
    AccountTruthInvariant,
    AccountTruthMessage,
    AccountTruthOrderCancelAction,
    AccountTruthOrderRow,
    AccountTruthOwnerBindingState,
    AccountTruthOwnerSummary,
    AccountTruthPositionRow,
    AccountTruthResponse,
    AccountTruthSeverity,
    AccountTruthSourceFreshness,
    AccountTruthSymbolExposure,
)
from app.schemas.operator_blocker import ConfirmInFormAction, OperatorBlocker, OperatorMove
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_TERMINAL_CANCEL_STATUSES = frozenset({"Cancelled", "ApiCancelled"})
_REJECTED_STATUSES = frozenset({"Inactive", "Rejected"})
_ACKNOWLEDGED_STATUSES = frozenset({"PreSubmitted", "Submitted"})


def _account_monitor_blockers(messages: Sequence[AccountTruthMessage]) -> list[OperatorBlocker]:
    """Project Account Truth messages into Account Monitor-scoped moves."""

    return [
        OperatorBlocker.for_host(
            condition_id=message.code,
            scope="account",
            host="account_monitor",
            disposition="fix_here",
            headline=message.title,
            detail=message.message,
            primary_move=OperatorMove(
                label="Run account reconcile",
                action=ConfirmInFormAction(
                    kind="confirm_in_form",
                    anchor="account-reconciliation-action",
                ),
            ),
            applies_to="both",
            severity="blocking" if message.severity == "critical" else "warning",
            evidence={
                key: value
                for key, value in message.forensic_facts.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            },
        )
        for message in messages
    ]
_LIMBO_STATUSES = frozenset({"PendingSubmit", "ApiPending", "PendingCancel", "Unknown"})


@dataclass(frozen=True)
class _NamespaceOwner:
    strategy_instance_id: str
    binding_state: AccountTruthOwnerBindingState


@dataclass(frozen=True)
class _NamespaceViews:
    attribution_by_namespace: dict[str, _NamespaceOwner]
    active_by_namespace: dict[str, _NamespaceOwner]
    duplicate_active_namespaces: frozenset[str]
    registry_unavailable: bool = False


class AccountInstanceRegistryEvidence(NamedTuple):
    bindings: list[AccountInstanceBinding]
    evidence_gaps: list[AccountTruthEvidenceGap]


@dataclass(frozen=True)
class AccountTruthCollectionContext:
    account_instance_bindings: tuple[AccountInstanceBinding, ...]
    evidence_gaps: tuple[AccountTruthEvidenceGap, ...]
    account_recovery_state: AccountRecoveryState


def load_account_instance_registry_evidence(
    *,
    artifacts_root: Path,
    account_id: str | None,
    context: str,
) -> AccountInstanceRegistryEvidence:
    """Read account registry bindings as Account Truth evidence."""
    if not account_id:
        return AccountInstanceRegistryEvidence(
            [],
            [
                AccountTruthEvidenceGap(
                    source="instance_registry",
                    severity="critical",
                    message="Account instance registry unavailable: broker account id is unknown",
                )
            ],
        )
    try:
        return AccountInstanceRegistryEvidence(
            read_account_instance_registry(artifacts_root, account_id),
            [],
        )
    except (AccountArtifactError, OSError, json.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            "failed to read account instance registry for account truth",
            extra={"account_id": account_id, "context": context, "exception": repr(exc)},
        )
        return AccountInstanceRegistryEvidence(
            [],
            [
                AccountTruthEvidenceGap(
                    source="instance_registry",
                    severity="critical",
                    message=f"Account instance registry unavailable: {exc}",
                )
            ],
        )


def build_account_truth_collection_context(
    *,
    artifacts_root: Path,
    account_id: str | None,
    context: str,
) -> AccountTruthCollectionContext:
    """Collect filesystem-backed Account Truth context once per request."""

    registry_evidence = load_account_instance_registry_evidence(
        artifacts_root=artifacts_root,
        account_id=account_id,
        context=context,
    )
    recovery_state = read_account_recovery_state(
        artifacts_root=artifacts_root,
        account_id=account_id,
    )
    evidence_gaps = list(registry_evidence.evidence_gaps)
    if recovery_state.status == "unreadable":
        evidence_gaps.append(
            AccountTruthEvidenceGap(
                source="account_freeze",
                severity="critical",
                message=f"Account freeze state unavailable: {recovery_state.unreadable_error}",
            )
        )
    return AccountTruthCollectionContext(
        account_instance_bindings=tuple(registry_evidence.bindings),
        evidence_gaps=tuple(evidence_gaps),
        account_recovery_state=recovery_state,
    )


async def fetch_account_truth(
    client: IbkrClient,
    *,
    health: IbkrConnectionHealth,
    collection_context: AccountTruthCollectionContext,
) -> AccountTruthResponse:
    """Collect broker facts and project them into account truth."""
    evidence_gaps = list(collection_context.evidence_gaps)

    account_summary, positions_snapshot, open_orders, completed_orders, executions = (
        await asyncio.gather(
            _collect_account_summary(client, evidence_gaps),
            _collect_positions(client, evidence_gaps),
            _collect_open_orders(client, evidence_gaps),
            _collect_completed_orders(client, evidence_gaps),
            _collect_executions(client, evidence_gaps),
        )
    )

    return compose_account_truth(
        health=health,
        account_instance_bindings=collection_context.account_instance_bindings,
        account=account_summary,
        positions_snapshot=positions_snapshot,
        open_orders=open_orders,
        completed_orders=completed_orders,
        executions=executions,
        evidence_gaps=evidence_gaps,
        account_recovery_state=collection_context.account_recovery_state,
        generated_at_ms=now_ms_utc(),
    )


async def _collect_account_summary(
    client: IbkrClient,
    gaps: list[AccountTruthEvidenceGap],
) -> IbkrAccountSummary | None:
    try:
        return await ibkr_account.fetch_account_summary(client)
    except BrokerError as exc:
        _log_evidence_gap("account_summary", "critical", exc)
        gaps.append(
            AccountTruthEvidenceGap(
                source="account_summary",
                severity="critical",
                message=f"IBKR account summary unavailable: {exc}",
            )
        )
        return None


async def _collect_positions(
    client: IbkrClient,
    gaps: list[AccountTruthEvidenceGap],
) -> IbkrPositionsSnapshot | None:
    try:
        return await ibkr_account.fetch_positions(client, allow_cache_fallback=True)
    except BrokerError as exc:
        _log_evidence_gap("positions", "critical", exc)
        gaps.append(
            AccountTruthEvidenceGap(
                source="positions",
                severity="critical",
                message=f"IBKR positions unavailable: {exc}",
            )
        )
        return None


async def _collect_open_orders(
    client: IbkrClient,
    gaps: list[AccountTruthEvidenceGap],
) -> list[IbkrOpenOrder]:
    try:
        return await list_open_orders(client)
    except BrokerError as exc:
        _log_evidence_gap("open_orders", "critical", exc)
        gaps.append(
            AccountTruthEvidenceGap(
                source="open_orders",
                severity="critical",
                message=f"IBKR open-order sweep unavailable: {exc}",
            )
        )
        return []


async def _collect_completed_orders(
    client: IbkrClient,
    gaps: list[AccountTruthEvidenceGap],
) -> list[IbkrOpenOrder]:
    try:
        return await list_completed_orders(client)
    except BrokerError as exc:
        _log_evidence_gap("completed_orders", "warning", exc)
        gaps.append(
            AccountTruthEvidenceGap(
                source="completed_orders",
                severity="warning",
                message=f"IBKR completed-order sweep unavailable: {exc}",
            )
        )
        return []


async def _collect_executions(
    client: IbkrClient,
    gaps: list[AccountTruthEvidenceGap],
) -> list[IbkrOrderEvent]:
    try:
        return await executions_for_reconnect_recovery(client)
    except BrokerError as exc:
        _log_evidence_gap("executions", "warning", exc)
        gaps.append(
            AccountTruthEvidenceGap(
                source="executions",
                severity="warning",
                message=f"IBKR execution sweep unavailable: {exc}",
            )
        )
        return []


def compose_account_truth(
    *,
    health: IbkrConnectionHealth,
    account_instance_bindings: Sequence[AccountInstanceBinding],
    account_recovery_state: AccountRecoveryState,
    account: IbkrAccountSummary | None,
    positions_snapshot: IbkrPositionsSnapshot | None,
    open_orders: Sequence[IbkrOpenOrder],
    completed_orders: Sequence[IbkrOpenOrder],
    executions: Sequence[IbkrOrderEvent],
    evidence_gaps: Sequence[AccountTruthEvidenceGap] = (),
    generated_at_ms: int | None = None,
) -> AccountTruthResponse:
    """Pure account-truth projection for tests and the live endpoint."""
    checked_at_ms = generated_at_ms or now_ms_utc()
    account_id, account_scope_gap = _account_id_from_sources(
        health=health,
        account=account,
        positions_snapshot=positions_snapshot,
        open_orders=open_orders,
        completed_orders=completed_orders,
        executions=executions,
    )
    projection_gaps = list(evidence_gaps)
    if account_scope_gap is not None:
        projection_gaps.append(account_scope_gap)
    registry_unavailable = any(gap.source == "instance_registry" for gap in projection_gaps)
    namespace_views = _namespace_views(
        account_instance_bindings,
        account_id=account_id,
        registry_unavailable=registry_unavailable,
    )
    known_bot_namespaces = sorted(namespace_views.attribution_by_namespace)

    open_order_rows = [
        _order_row(
            order,
            fact_kind="open_order",
            namespace_views=namespace_views,
            health=health,
            account_recovery_state=account_recovery_state,
        )
        for order in open_orders
    ]
    completed_order_rows = [
        _order_row(
            order,
            fact_kind="completed_order",
            namespace_views=namespace_views,
            health=health,
            account_recovery_state=account_recovery_state,
        )
        for order in completed_orders
    ]
    order_rows = [*open_order_rows, *completed_order_rows]

    execution_rows, duplicate_exec_count = _execution_rows(
        executions,
        namespace_views=namespace_views,
    )
    position_rows = _position_rows(
        positions_snapshot,
        known_owners_by_con_id=_known_owners_by_con_id(order_rows, execution_rows),
        foreign_con_ids=_foreign_con_ids(order_rows, execution_rows),
    )

    manual_namespaces = sorted(
        {
            namespace
            for namespace in (
                _namespace_or_none(row.order_ref)
                for row in [*order_rows, *execution_rows]
            )
            if namespace is not None and _is_manual_namespace(namespace)
        }
    )

    blockers, caveats = _messages(
        order_rows=order_rows,
        execution_rows=execution_rows,
        position_rows=position_rows,
        evidence_gaps=projection_gaps,
        duplicate_exec_count=duplicate_exec_count,
        duplicate_active_namespaces=namespace_views.duplicate_active_namespaces,
    )
    invariants = _invariants(
        health=health,
        open_orders=open_order_rows,
        completed_orders=completed_order_rows,
        executions=execution_rows,
        positions=position_rows,
        evidence_gaps=projection_gaps,
        duplicate_exec_count=duplicate_exec_count,
        checked_at_ms=checked_at_ms,
    )
    owner_summaries = _owner_summaries(order_rows, execution_rows, position_rows)
    symbol_exposures = _symbol_exposures(position_rows)
    source_freshness = compose_account_truth_source_freshness(
        health=health,
        account=account,
        positions_snapshot=positions_snapshot,
        open_orders=open_orders,
        completed_orders=completed_orders,
        executions=executions,
        evidence_gaps=projection_gaps,
        checked_at_ms=checked_at_ms,
    )
    freshness_blockers, freshness_caveats = _source_freshness_messages(
        source_freshness,
        evidence_gaps=projection_gaps,
        checked_at_ms=checked_at_ms,
    )
    blockers.extend(freshness_blockers)
    caveats.extend(freshness_caveats)
    final_verdict, final_severity = _final_verdict(
        invariants,
        blockers=blockers,
        evidence_gaps=projection_gaps,
        source_freshness=source_freshness,
        checked_at_ms=checked_at_ms,
    )
    return AccountTruthResponse(
        account_id=account_id,
        final_verdict=final_verdict,
        final_severity=final_severity,
        status_label="Clean" if final_verdict == "clean" else "Not proven",
        status_detail=_status_detail(final_verdict, final_severity),
        generated_at_ms=checked_at_ms,
        health=health,
        account=account,
        known_bot_namespaces=known_bot_namespaces,
        manual_namespaces_observed=manual_namespaces,
        invariants=invariants,
        blockers=blockers,
        operator_blockers=_account_monitor_blockers(blockers),
        caveats=caveats,
        owner_summaries=owner_summaries,
        symbol_exposures=symbol_exposures,
        orders=order_rows,
        executions=execution_rows,
        positions=position_rows,
        evidence_gaps=projection_gaps,
        source_freshness=source_freshness,
    )


def _account_id_from_sources(
    *,
    health: IbkrConnectionHealth,
    account: IbkrAccountSummary | None,
    positions_snapshot: IbkrPositionsSnapshot | None,
    open_orders: Sequence[IbkrOpenOrder],
    completed_orders: Sequence[IbkrOpenOrder],
    executions: Sequence[IbkrOrderEvent],
) -> tuple[str | None, AccountTruthEvidenceGap | None]:
    account_ids: list[str] = []
    if account is not None and account.account_id:
        account_ids.append(account.account_id)
    if positions_snapshot is not None and positions_snapshot.account_id:
        account_ids.append(positions_snapshot.account_id)
    if health.account_id:
        account_ids.append(health.account_id)
    account_ids.extend(order.account_id for order in open_orders if order.account_id)
    account_ids.extend(order.account_id for order in completed_orders if order.account_id)
    account_ids.extend(event.account_id for event in executions if event.account_id)

    by_normalized = {account_id.upper(): account_id for account_id in account_ids}
    if len(by_normalized) == 1:
        return next(iter(by_normalized.values())), None
    if not by_normalized:
        return None, None
    return (
        None,
        AccountTruthEvidenceGap(
            source="account_scope",
            severity="critical",
            message="Broker evidence contains conflicting account ids; account registry ownership cannot be proven.",
        ),
    )


def _namespace_views(
    bindings: Sequence[AccountInstanceBinding],
    *,
    account_id: str | None,
    registry_unavailable: bool,
) -> _NamespaceViews:
    if account_id is None:
        binding_index = index_account_instance_bindings(())
    else:
        binding_index = index_account_instance_bindings(bindings, account_id=account_id)

    attribution_by_namespace = {
        namespace: _namespace_owner(binding)
        for namespace, binding in binding_index.latest_by_namespace.items()
    }
    active_by_namespace = {
        namespace: _namespace_owner(namespace_bindings[0])
        for namespace, namespace_bindings in binding_index.active_by_namespace.items()
        if len(namespace_bindings) == 1
    }
    return _NamespaceViews(
        attribution_by_namespace=attribution_by_namespace,
        active_by_namespace=active_by_namespace,
        duplicate_active_namespaces=binding_index.duplicate_active_namespaces,
        registry_unavailable=registry_unavailable,
    )


def _namespace_owner(binding: AccountInstanceBinding) -> _NamespaceOwner:
    return _NamespaceOwner(
        strategy_instance_id=binding.strategy_instance_id,
        binding_state=binding.lifecycle_state,
    )


def cancel_action_for_open_order(
    order: IbkrOpenOrder,
    *,
    health: IbkrConnectionHealth,
    collection_context: AccountTruthCollectionContext,
) -> AccountTruthOrderCancelAction:
    """Project the canonical cancel action for one current open-order fact."""

    registry_unavailable = any(
        gap.source == "instance_registry" for gap in collection_context.evidence_gaps
    )
    namespace_views = _namespace_views(
        collection_context.account_instance_bindings,
        account_id=health.account_id or order.account_id,
        registry_unavailable=registry_unavailable,
    )
    owner = _live_risk_owner(_owner_for_order_ref(order.order_ref, namespace_views))
    lifecycle = _order_lifecycle(order.status, order.remaining)
    return evaluate_order_cancel_capability(
        health=health,
        fact_kind="open_order",
        owner=owner,
        lifecycle=lifecycle,
        remaining=order.remaining,
        account_recovery_state=collection_context.account_recovery_state,
    )


def _order_row(
    order: IbkrOpenOrder,
    *,
    fact_kind: str,
    namespace_views: _NamespaceViews,
    health: IbkrConnectionHealth,
    account_recovery_state: AccountRecoveryState,
) -> AccountTruthOrderRow:
    owner = _owner_for_order_ref(order.order_ref, namespace_views)
    lifecycle = _order_lifecycle(order.status, order.remaining)
    if fact_kind == "open_order":
        owner = _live_risk_owner(owner)
    elif fact_kind == "completed_order" and owner.owner_class == "foreign_or_unclaimed":
        owner = owner.model_copy(update={"severity": "warning"})
    lifecycle_id = (
        f"perm:{order.perm_id}"
        if order.perm_id is not None
        else f"account:{order.account_id}:client:{order.client_id}:order:{order.order_id}"
    )
    return AccountTruthOrderRow(
        fact_kind=fact_kind,  # type: ignore[arg-type]
        lifecycle_id=lifecycle_id,
        lifecycle=lifecycle,
        account_id=order.account_id,
        order_id=order.order_id,
        perm_id=order.perm_id,
        client_id=order.client_id,
        con_id=order.con_id,
        symbol=order.symbol,
        sec_type=order.sec_type,
        action=order.action,
        quantity=order.quantity,
        order_type=order.order_type,
        limit_price=order.limit_price,
        status=order.status,
        cumulative_filled=order.cumulative_filled,
        remaining=order.remaining,
        avg_fill_price=order.avg_fill_price,
        order_ref=order.order_ref,
        owner=owner,
        cancel_action=evaluate_order_cancel_capability(
            health=health,
            fact_kind=fact_kind,
            owner=owner,
            lifecycle=lifecycle,
            remaining=order.remaining,
            account_recovery_state=account_recovery_state,
        ),
        headline=_fact_headline(owner, fact_kind.replace("_", " ")),
        detail=_order_detail(order, owner),
        fetched_at_ms=order.fetched_at_ms,
        ibkr_evidence=order.ibkr_evidence,
    )


def _execution_rows(
    executions: Sequence[IbkrOrderEvent],
    *,
    namespace_views: _NamespaceViews,
) -> tuple[list[AccountTruthExecutionRow], int]:
    rows_by_exec_id: dict[str, AccountTruthExecutionRow] = {}
    ordered_exec_ids: list[str] = []
    duplicate_count = 0
    for event in executions:
        if not event.exec_id:
            continue
        row = _execution_row(event, namespace_views=namespace_views)
        existing = rows_by_exec_id.get(event.exec_id)
        if existing is not None:
            duplicate_count += 1
            rows_by_exec_id[event.exec_id] = _merge_execution_row(existing, row)
            continue
        rows_by_exec_id[event.exec_id] = row
        ordered_exec_ids.append(event.exec_id)
    return [rows_by_exec_id[exec_id] for exec_id in ordered_exec_ids], duplicate_count


def _execution_row(
    event: IbkrOrderEvent,
    *,
    namespace_views: _NamespaceViews,
) -> AccountTruthExecutionRow:
    owner = _owner_for_order_ref(event.order_ref, namespace_views)
    if owner.owner_class == "foreign_or_unclaimed":
        owner = owner.model_copy(update={"severity": "critical"})
    price = event.last_fill_price if event.last_fill_price is not None else event.avg_fill_price
    return AccountTruthExecutionRow(
        account_id=event.account_id,
        exec_id=event.exec_id,
        order_id=event.order_id,
        perm_id=event.perm_id,
        client_id=event.client_id,
        con_id=event.con_id,
        symbol=event.symbol,
        side=event.side,
        order_type=event.order_type,
        quantity=event.fill_quantity,
        price=price,
        fee=event.fee,
        exec_time_ms=event.exec_time_ms,
        observed_at_ms=event.ts_ms,
        order_ref=event.order_ref,
        owner=owner,
        headline=_fact_headline(owner, "execution"),
        detail=_execution_detail(event, owner),
        uncertainty_codes=_execution_uncertainty_codes(
            order_ref=event.order_ref,
            exec_time_ms=event.exec_time_ms,
            fee=event.fee,
            quantity=event.fill_quantity,
            price=price,
        ),
        ibkr_evidence=event.ibkr_evidence,
    )


def _execution_uncertainty_codes(
    *,
    order_ref: str | None,
    exec_time_ms: int | None,
    fee: float | None,
    quantity: float | None,
    price: float | None,
) -> list[AccountTruthExecutionUncertaintyCode]:
    codes: list[AccountTruthExecutionUncertaintyCode] = []
    if not order_ref:
        codes.append("missing_order_ref")
    if exec_time_ms is None:
        codes.append("observed_time_only")
    if fee is None:
        codes.append("commission_pending")
    if quantity is None:
        codes.append("missing_quantity")
    if price is None:
        codes.append("missing_price")
    return codes


def _merge_execution_row(
    existing: AccountTruthExecutionRow,
    incoming: AccountTruthExecutionRow,
) -> AccountTruthExecutionRow:
    updates: dict[str, object] = {}
    for field_name in (
        "perm_id",
        "client_id",
        "con_id",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "price",
        "fee",
        "exec_time_ms",
        "order_ref",
        "ibkr_evidence",
    ):
        if getattr(existing, field_name) is None and getattr(incoming, field_name) is not None:
            updates[field_name] = getattr(incoming, field_name)

    if (
        existing.owner.owner_class == "foreign_or_unclaimed"
        and incoming.owner.owner_class != "foreign_or_unclaimed"
    ):
        updates["owner"] = incoming.owner
        updates["headline"] = incoming.headline
        updates["detail"] = incoming.detail

    if not updates:
        return existing
    merged = existing.model_copy(update=updates)
    return merged.model_copy(
        update={
            "uncertainty_codes": _execution_uncertainty_codes(
                order_ref=merged.order_ref,
                exec_time_ms=merged.exec_time_ms,
                fee=merged.fee,
                quantity=merged.quantity,
                price=merged.price,
            )
        }
    )


def _position_rows(
    positions_snapshot: IbkrPositionsSnapshot | None,
    *,
    known_owners_by_con_id: dict[int, set[AccountTruthFactOwner]],
    foreign_con_ids: set[int],
) -> list[AccountTruthPositionRow]:
    if positions_snapshot is None:
        return []
    rows: list[AccountTruthPositionRow] = []
    for position in positions_snapshot.positions:
        if position.con_id in foreign_con_ids:
            owner = _foreign_owner("unclaimed broker exposure", severity="critical")
        else:
            owner_keys = known_owners_by_con_id.get(position.con_id, set())
            if len(owner_keys) == 1:
                owner = _live_risk_owner(next(iter(owner_keys)))
            elif len(owner_keys) > 1:
                owner_binding_state = _mixed_owner_binding_state(owner_keys)
                owner = AccountTruthFactOwner(
                    owner_class="mixed_known",
                    owner_key="mixed",
                    owner_label="Mixed known owners",
                    evidence_tier="mixed_known",
                    evidence_label="Mixed known evidence",
                    owner_binding_state=owner_binding_state,
                    severity="critical" if owner_binding_state == "RETIRED" else "warning",
                )
            else:
                owner = _foreign_owner("unclaimed broker exposure", severity="critical")
        rows.append(
            AccountTruthPositionRow(
                account_id=position.account_id,
                con_id=position.con_id,
                symbol=position.symbol,
                sec_type=position.sec_type,
                quantity=position.quantity,
                avg_cost=position.avg_cost,
                market_value=position.market_value,
                owner=owner,
                headline=_fact_headline(owner, "position"),
                detail=_position_detail(position, owner),
                fetched_at_ms=position.fetched_at_ms,
            )
        )
    return rows


def _owner_for_order_ref(
    order_ref: str | None,
    namespace_views: _NamespaceViews,
) -> AccountTruthFactOwner:
    if order_ref is None:
        return _foreign_owner("missing order_ref")
    try:
        namespace, _intent_id = parse_order_ref(order_ref)
    except ValueError:
        return _foreign_owner("unparseable order_ref")
    if namespace in namespace_views.duplicate_active_namespaces:
        return AccountTruthFactOwner(
            owner_class="mixed_known",
            owner_key="duplicate_active_namespace",
            owner_label="Duplicate active namespace",
            evidence_tier="mixed_known",
            evidence_label="Ambiguous active registry namespace",
            owner_binding_state="UNKNOWN",
            severity="critical",
        )
    namespace_owner = namespace_views.active_by_namespace.get(
        namespace,
        namespace_views.attribution_by_namespace.get(namespace),
    )
    if namespace_owner is not None:
        sid = namespace_owner.strategy_instance_id
        return AccountTruthFactOwner(
            owner_class="bot",
            owner_key=sid,
            owner_label=f"Bot {sid}",
            evidence_tier="bot_order_ref",
            evidence_label="Bot-stamped order ref",
            owner_binding_state=namespace_owner.binding_state,
            severity="ok",
        )
    if namespace_views.registry_unavailable and _is_bot_namespace(namespace):
        sid = _strategy_instance_id_from_bot_namespace(namespace)
        return AccountTruthFactOwner(
            owner_class="bot",
            owner_key=sid,
            owner_label=f"Bot {sid}",
            evidence_tier="bot_order_ref",
            evidence_label="Bot-stamped order ref; registry unavailable",
            owner_binding_state="UNKNOWN",
            severity="critical",
        )
    if _is_manual_namespace(namespace):
        operator = namespace.split(NAMESPACE_SEP)[1]
        return AccountTruthFactOwner(
            owner_class="manual",
            owner_key=operator,
            owner_label=f"Manual {operator}",
            evidence_tier="app_minted_manual",
            evidence_label="App-minted manual order ref",
            owner_binding_state="UNKNOWN",
            severity="ok",
        )
    return _foreign_owner("namespace is not registered")


def _live_risk_owner(owner: AccountTruthFactOwner) -> AccountTruthFactOwner:
    if owner.owner_class == "foreign_or_unclaimed":
        return owner.model_copy(update={"severity": "critical"})
    if _is_retired_owner(owner):
        return owner.model_copy(update={"severity": "critical"})
    return owner


def _is_retired_owner(owner: AccountTruthFactOwner) -> bool:
    return owner.owner_class in {"bot", "mixed_known"} and owner.owner_binding_state == "RETIRED"


def _mixed_owner_binding_state(
    owners: set[AccountTruthFactOwner],
) -> AccountTruthOwnerBindingState:
    states = {owner.owner_binding_state for owner in owners}
    if "ACTIVE" in states:
        return "ACTIVE"
    if "DEPLOYED" in states:
        return "DEPLOYED"
    if states == {"RETIRED"}:
        return "RETIRED"
    return "UNKNOWN"


def _foreign_owner(reason: str, *, severity: AccountTruthSeverity = "warning") -> AccountTruthFactOwner:
    return AccountTruthFactOwner(
        owner_class="foreign_or_unclaimed",
        owner_key="foreign_or_unclaimed",
        owner_label="Foreign or unclaimed",
        evidence_tier="foreign_or_unclaimed",
        evidence_label="No known ownership evidence",
        owner_binding_state="UNKNOWN",
        severity=severity,
    )


def _namespace_or_none(order_ref: str | None) -> str | None:
    if order_ref is None:
        return None
    try:
        namespace, _intent_id = parse_order_ref(order_ref)
    except ValueError:
        return None
    return namespace


def _is_manual_namespace(namespace: str) -> bool:
    parts = namespace.split(NAMESPACE_SEP)
    return (
        len(parts) == 3
        and parts[0] == MANUAL_NAMESPACE_ROOT
        and parts[1] != ""
        and parts[2] == NAMESPACE_VERSION
    )


def _is_bot_namespace(namespace: str) -> bool:
    parts = namespace.split(NAMESPACE_SEP)
    return (
        len(parts) == 3
        and parts[0] == NAMESPACE_ROOT
        and parts[1] != ""
        and parts[2] == NAMESPACE_VERSION
    )


def _strategy_instance_id_from_bot_namespace(namespace: str) -> str:
    return namespace.split(NAMESPACE_SEP)[1]


def _order_lifecycle(status: str, remaining: float) -> str:
    if status in _TERMINAL_CANCEL_STATUSES:
        return "cancelled"
    if status in _REJECTED_STATUSES:
        return "rejected"
    if status == "Filled" or remaining == 0:
        return "filled"
    if status in _ACKNOWLEDGED_STATUSES:
        return "acknowledged"
    if status in _LIMBO_STATUSES:
        return "limbo"
    return "submitted"


def _fact_headline(owner: AccountTruthFactOwner, label: str) -> str:
    if owner.owner_class == "foreign_or_unclaimed":
        return f"Unclaimed broker {label}"
    if owner.owner_class == "mixed_known":
        if owner.owner_binding_state == "RETIRED":
            return f"Retired mixed-owner {label}"
        return f"Known mixed-owner {label}"
    if _is_retired_owner(owner):
        return f"Retired {owner.owner_label} {label}"
    return f"{owner.owner_label} {label}"


def _order_detail(order: IbkrOpenOrder, owner: AccountTruthFactOwner) -> str:
    base = (
        f"IBKR reports order {order.order_id} for {order.action} "
        f"{order.quantity:g} {order.symbol} as {order.status}."
    )
    if owner.owner_class == "foreign_or_unclaimed":
        return f"{base} No exact known namespace proves ownership."
    if _is_retired_owner(owner) and owner.severity == "critical":
        return (
            f"{base} Ownership is proven by {owner.evidence_tier}, "
            "but the owning binding is retired while exposure remains live."
        )
    if _is_retired_owner(owner):
        return (
            f"{base} Ownership is proven by {owner.evidence_tier}; "
            "the historical owner binding is retired."
        )
    return f"{base} Ownership is proven by {owner.evidence_tier}."


def _execution_detail(event: IbkrOrderEvent, owner: AccountTruthFactOwner) -> str:
    quantity = f"{event.fill_quantity:g}" if event.fill_quantity is not None else "unknown quantity"
    base = (
        f"IBKR execution {event.exec_id} filled "
        f"{quantity} {event.symbol or 'unknown symbol'}."
    )
    if owner.owner_class == "foreign_or_unclaimed":
        return f"{base} No exact known namespace proves ownership."
    if _is_retired_owner(owner):
        return (
            f"{base} Ownership is proven by {owner.evidence_tier}; "
            "the historical owner binding is retired."
        )
    return f"{base} Ownership is proven by {owner.evidence_tier}."


def _position_detail(position: IbkrPosition, owner: AccountTruthFactOwner) -> str:
    if _is_retired_owner(owner):
        return f"{position.symbol} position is attributed to retired {owner.owner_label}."
    if owner.owner_class != "foreign_or_unclaimed":
        return f"{position.symbol} position is attributed to {owner.owner_label}."
    return f"{position.symbol} position has no known bot/manual evidence."


def _known_owners_by_con_id(
    order_rows: Iterable[AccountTruthOrderRow],
    execution_rows: Iterable[AccountTruthExecutionRow],
) -> dict[int, set[AccountTruthFactOwner]]:
    owners: dict[int, set[AccountTruthFactOwner]] = defaultdict(set)
    for row in order_rows:
        if row.cumulative_filled > 0 and row.owner.owner_class in {"bot", "manual"}:
            owners[row.con_id].add(row.owner)
    for row in execution_rows:
        if row.con_id is None:
            continue
        if row.owner.owner_class in {"bot", "manual"}:
            owners[row.con_id].add(row.owner)
    return owners


def _foreign_con_ids(
    order_rows: Iterable[AccountTruthOrderRow],
    execution_rows: Iterable[AccountTruthExecutionRow],
) -> set[int]:
    out: set[int] = set()
    for row in order_rows:
        if row.cumulative_filled > 0 and row.owner.owner_class == "foreign_or_unclaimed":
            out.add(row.con_id)
    for row in execution_rows:
        if row.con_id is not None and row.owner.owner_class == "foreign_or_unclaimed":
            out.add(row.con_id)
    return out


def _messages(
    *,
    order_rows: Sequence[AccountTruthOrderRow],
    execution_rows: Sequence[AccountTruthExecutionRow],
    position_rows: Sequence[AccountTruthPositionRow],
    evidence_gaps: Sequence[AccountTruthEvidenceGap],
    duplicate_exec_count: int,
    duplicate_active_namespaces: frozenset[str],
) -> tuple[list[AccountTruthMessage], list[AccountTruthMessage]]:
    blockers: list[AccountTruthMessage] = []
    caveats: list[AccountTruthMessage] = []
    if duplicate_active_namespaces:
        blockers.append(
            AccountTruthMessage(
                code="duplicate_active_namespace",
                severity="critical",
                title="Duplicate active bot namespace",
                message=(
                    "The account instance registry has more than one active binding for "
                    "the same bot order namespace."
                ),
                forensic_facts={
                    "namespaces": sorted(duplicate_active_namespaces),
                    "count": len(duplicate_active_namespaces),
                },
            )
        )
    unknown_open = [
        row for row in order_rows
        if row.fact_kind == "open_order" and row.owner.owner_class == "foreign_or_unclaimed"
    ]
    if unknown_open:
        blockers.append(
            AccountTruthMessage(
                code="unknown_open_orders",
                severity="critical",
                title="Unknown open broker orders",
                message="At least one live IBKR order has no known bot or manual namespace.",
                forensic_facts={"count": len(unknown_open)},
            )
        )
    unknown_positions = [
        row for row in position_rows
        if row.owner.owner_class == "foreign_or_unclaimed"
    ]
    if unknown_positions:
        blockers.append(
            AccountTruthMessage(
                code="unknown_positions",
                severity="critical",
                title="Unknown current broker positions",
                message="At least one current IBKR position is not explained by known bot/manual evidence.",
                forensic_facts={"count": len(unknown_positions)},
            )
        )
    retired_live_orders = [
        row for row in order_rows
        if row.fact_kind == "open_order" and _is_retired_owner(row.owner)
    ]
    retired_live_positions = [
        row for row in position_rows
        if _is_retired_owner(row.owner)
    ]
    if retired_live_orders or retired_live_positions:
        blockers.append(
            AccountTruthMessage(
                code="retired_owner_live_exposure",
                severity="critical",
                title="Retired owner still has live exposure",
                message=(
                    "A known retired bot owns a live broker order or current position; "
                    "the exposure is attributable but no active binding is managing it."
                ),
                forensic_facts={
                    "open_order_count": len(retired_live_orders),
                    "position_count": len(retired_live_positions),
                },
            )
        )
    unknown_execs = [
        row for row in execution_rows
        if row.owner.owner_class == "foreign_or_unclaimed"
    ]
    if unknown_execs:
        blockers.append(
            AccountTruthMessage(
                code="unknown_executions",
                severity="critical",
                title="Unknown executions",
                message="At least one IBKR execution has no known bot or manual namespace.",
                forensic_facts={"count": len(unknown_execs)},
            )
        )
    missing_commissions = [row for row in execution_rows if row.fee is None]
    if missing_commissions:
        caveats.append(
            AccountTruthMessage(
                code="missing_commission",
                severity="warning",
                title="Commission evidence pending",
                message="One or more executions are missing IBKR commission reports.",
                forensic_facts={"count": len(missing_commissions)},
            )
        )
    if duplicate_exec_count:
        caveats.append(
            AccountTruthMessage(
                code="duplicate_exec_id_suppressed",
                severity="info",
                title="Duplicate execution redelivery suppressed",
                message=(
                    "IBKR redelivered one or more execIds; account truth kept the first "
                    "observation and backfilled missing fields from later observations."
                ),
                forensic_facts={"count": duplicate_exec_count},
            )
        )
    for gap in evidence_gaps:
        target = blockers if gap.severity == "critical" else caveats
        target.append(
            AccountTruthMessage(
                code=f"evidence_gap_{gap.source}",
                severity=gap.severity,
                title="Broker evidence source unavailable",
                message=gap.message,
                forensic_facts={"source": gap.source},
            )
        )
    return blockers, caveats


def _source_freshness_messages(
    source_freshness: Sequence[AccountTruthSourceFreshness],
    *,
    evidence_gaps: Sequence[AccountTruthEvidenceGap],
    checked_at_ms: int,
) -> tuple[list[AccountTruthMessage], list[AccountTruthMessage]]:
    gap_sources = {gap.source for gap in evidence_gaps}
    blockers: list[AccountTruthMessage] = []
    caveats: list[AccountTruthMessage] = []
    for row in critical_source_freshness_blocks(source_freshness, checked_at_ms=checked_at_ms):
        if row.source in gap_sources:
            continue
        blockers.append(_source_freshness_message(row))
    for row in source_freshness:
        if row.severity == "critical" or row.status == "fresh" or row.source in gap_sources:
            continue
        caveats.append(_source_freshness_message(row))
    return blockers, caveats


def _source_freshness_message(row: AccountTruthSourceFreshness) -> AccountTruthMessage:
    return AccountTruthMessage(
        code=f"source_freshness_{row.source}_{row.status}",
        severity=row.severity,
        title=f"{row.label} evidence is {row.status}",
        message=row.message,
        forensic_facts={
            "source": row.source,
            "status": row.status,
            "reason_code": row.reason_code,
            "fetched_at_ms": row.fetched_at_ms,
            "age_ms": row.age_ms,
            "hard_ttl_ms": row.hard_ttl_ms,
        },
    )


def _invariants(
    *,
    health: IbkrConnectionHealth,
    open_orders: Sequence[AccountTruthOrderRow],
    completed_orders: Sequence[AccountTruthOrderRow],
    executions: Sequence[AccountTruthExecutionRow],
    positions: Sequence[AccountTruthPositionRow],
    evidence_gaps: Sequence[AccountTruthEvidenceGap],
    duplicate_exec_count: int,
    checked_at_ms: int,
) -> list[AccountTruthInvariant]:
    unknown_open = [
        row for row in open_orders
        if row.fact_kind == "open_order" and row.owner.owner_class == "foreign_or_unclaimed"
    ]
    unknown_completed = [
        row for row in completed_orders
        if row.fact_kind == "completed_order" and row.owner.owner_class == "foreign_or_unclaimed"
    ]
    unknown_execs = [
        row for row in executions if row.owner.owner_class == "foreign_or_unclaimed"
    ]
    unknown_positions = [
        row for row in positions if row.owner.owner_class == "foreign_or_unclaimed"
    ]
    retired_open = [
        row for row in open_orders
        if row.fact_kind == "open_order" and _is_retired_owner(row.owner)
    ]
    retired_positions = [
        row for row in positions if _is_retired_owner(row.owner)
    ]
    missing_commissions = [row for row in executions if row.fee is None]
    gap_sources = {gap.source: gap for gap in evidence_gaps}
    liveness_ok = (
        health.connected
        and not health.connection_lost
        and health.connection_state == "connected"
    )
    return [
        _invariant(
            "broker_liveness_proven",
            "Broker liveness proven",
            ok=liveness_ok,
            fail_severity="critical",
            checked_at_ms=checked_at_ms,
            evidence_count=1,
            fail_text="The broker connection is not live enough to prove account truth.",
            pass_text="The broker connection is live.",
        ),
        _invariant(
            "open_orders_known",
            "Open orders known",
            ok=not unknown_open and not retired_open and "open_orders" not in gap_sources,
            fail_severity="critical",
            checked_at_ms=checked_at_ms,
            evidence_count=len(open_orders),
            fail_text="One or more live open orders are foreign, unclaimed, or owned by a retired binding.",
            pass_text="Every live open order has known ownership.",
        ),
        _invariant(
            "completed_orders_known",
            "Completed orders known",
            ok=not unknown_completed and "completed_orders" not in gap_sources,
            fail_severity="warning",
            checked_at_ms=checked_at_ms,
            evidence_count=len(completed_orders),
            fail_text="Completed-order history is incomplete or has unclaimed rows.",
            pass_text="Recent completed orders have known ownership.",
        ),
        _invariant(
            "all_executions_assigned",
            "All executions assigned",
            ok=not unknown_execs and "executions" not in gap_sources,
            fail_severity="critical",
            checked_at_ms=checked_at_ms,
            evidence_count=len(executions),
            fail_text="One or more executions are foreign or unclaimed.",
            pass_text="Every execution is assigned to a known owner.",
        ),
        _invariant(
            "positions_match_known_ownership",
            "Positions match known ownership",
            ok=not unknown_positions and not retired_positions and "positions" not in gap_sources,
            fail_severity="critical",
            checked_at_ms=checked_at_ms,
            evidence_count=len(positions),
            fail_text="One or more current positions are unexplained or owned by a retired binding.",
            pass_text="Current positions are explained by known ownership evidence.",
        ),
        AccountTruthInvariant(
            key="commission_complete",
            label="Commission complete",
            status="warn" if missing_commissions else "pass",
            severity="warning" if missing_commissions else "ok",
            headline=(
                "Commission reports pending"
                if missing_commissions
                else "Commission evidence complete"
            ),
            narrative=(
                "At least one execution is missing IBKR commission evidence."
                if missing_commissions
                else "Every observed execution has commission evidence or no execution was observed."
            ),
            checked_at_ms=checked_at_ms,
            evidence_count=len(executions),
        ),
        AccountTruthInvariant(
            key="flex_audit_match",
            label="Flex audit match",
            status="not_applicable",
            severity="info",
            headline="Flex audit not imported yet",
            narrative="Flex statements are the delayed official audit source and are not part of the live MVP projection.",
            checked_at_ms=checked_at_ms,
            evidence_count=0,
        ),
        AccountTruthInvariant(
            key="duplicate_exec_id_suppressed",
            label="Duplicate execIds suppressed",
            status="warn" if duplicate_exec_count else "pass",
            severity="info" if duplicate_exec_count else "ok",
            headline=(
                "Duplicate execIds suppressed"
                if duplicate_exec_count
                else "No duplicate execIds observed"
            ),
            narrative=(
                "IBKR redelivered executions; account truth deduped by execId."
                if duplicate_exec_count
                else "No duplicate execution redeliveries were observed in this projection."
            ),
            checked_at_ms=checked_at_ms,
            evidence_count=duplicate_exec_count,
        ),
    ]


def _invariant(
    key: str,
    label: str,
    *,
    ok: bool,
    fail_severity: AccountTruthSeverity,
    checked_at_ms: int,
    evidence_count: int,
    fail_text: str,
    pass_text: str,
) -> AccountTruthInvariant:
    return AccountTruthInvariant(
        key=key,
        label=label,
        status="pass" if ok else "fail",
        severity="ok" if ok else fail_severity,
        headline=pass_text if ok else fail_text,
        narrative=pass_text if ok else fail_text,
        checked_at_ms=checked_at_ms,
        evidence_count=evidence_count,
    )


def _owner_summaries(
    order_rows: Sequence[AccountTruthOrderRow],
    execution_rows: Sequence[AccountTruthExecutionRow],
    position_rows: Sequence[AccountTruthPositionRow],
) -> list[AccountTruthOwnerSummary]:
    aggregate: dict[tuple[str, str, str, str, str, str], dict[str, float]] = defaultdict(
        lambda: {
            "open_order_count": 0,
            "execution_count": 0,
            "position_count": 0,
            "gross_position_quantity": 0.0,
        }
    )
    for row in order_rows:
        key = _summary_key(row.owner)
        if row.fact_kind == "open_order":
            aggregate[key]["open_order_count"] += 1
    for row in execution_rows:
        aggregate[_summary_key(row.owner)]["execution_count"] += 1
    for row in position_rows:
        bucket = aggregate[_summary_key(row.owner)]
        bucket["position_count"] += 1
        bucket["gross_position_quantity"] += abs(row.quantity)
    return [
        AccountTruthOwnerSummary(
            owner_class=owner_class,  # type: ignore[arg-type]
            owner_key=owner_key,
            owner_label=owner_label,
            evidence_tier=evidence_tier,  # type: ignore[arg-type]
            evidence_label=evidence_label,
            owner_binding_state=owner_binding_state,  # type: ignore[arg-type]
            open_order_count=int(values["open_order_count"]),
            execution_count=int(values["execution_count"]),
            position_count=int(values["position_count"]),
            gross_position_quantity=values["gross_position_quantity"],
        )
        for (
            owner_class,
            owner_key,
            owner_label,
            evidence_tier,
            evidence_label,
            owner_binding_state,
        ), values in sorted(aggregate.items())
    ]


def _summary_key(owner: AccountTruthFactOwner) -> tuple[str, str, str, str, str, str]:
    return (
        owner.owner_class,
        owner.owner_key,
        owner.owner_label,
        owner.evidence_tier,
        owner.evidence_label,
        owner.owner_binding_state,
    )


def _symbol_exposures(
    position_rows: Sequence[AccountTruthPositionRow],
) -> list[AccountTruthSymbolExposure]:
    return [
        AccountTruthSymbolExposure(
            symbol=row.symbol,
            owner_class=row.owner.owner_class,
            owner_key=row.owner.owner_key,
            owner_label=row.owner.owner_label,
            quantity=row.quantity,
            con_id=row.con_id,
        )
        for row in position_rows
    ]


def _final_verdict(
    invariants: Sequence[AccountTruthInvariant],
    *,
    blockers: Sequence[AccountTruthMessage],
    evidence_gaps: Sequence[AccountTruthEvidenceGap],
    source_freshness: Sequence[AccountTruthSourceFreshness],
    checked_at_ms: int,
) -> tuple[AccountTruthFinalVerdict, AccountTruthSeverity]:
    if any(gap.severity == "critical" for gap in evidence_gaps) or any(
        blocker.severity == "critical" for blocker in blockers
    ) or critical_source_freshness_blocks(source_freshness, checked_at_ms=checked_at_ms):
        return "not_proven", "critical"
    failing = [row for row in invariants if row.status == "fail"]
    if not failing:
        warning = any(row.status == "warn" for row in invariants)
        return ("clean", "warning" if warning else "ok")
    if any(row.severity == "critical" for row in failing):
        return "not_proven", "critical"
    return "not_proven", "warning"


def _status_detail(
    final_verdict: AccountTruthFinalVerdict,
    final_severity: AccountTruthSeverity,
) -> str:
    if final_verdict == "clean" and final_severity == "ok":
        return "Required live broker evidence is assigned to known ownership."
    if final_verdict == "clean":
        return "Ownership is assigned, but one or more caveats still need review."
    if final_severity == "critical":
        return "Bot submits should stay blocked until critical account truth blockers clear."
    return "Account truth is degraded and needs review before calling the account clean."


def _log_evidence_gap(
    source: str,
    severity: AccountTruthSeverity,
    exc: BrokerError,
) -> None:
    logger.warning(
        "IBKR account truth evidence source unavailable",
        extra={
            "ibkr_source": source,
            "account_truth_severity": severity,
            "error": str(exc),
        },
    )
