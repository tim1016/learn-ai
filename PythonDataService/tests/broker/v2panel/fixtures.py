"""Shared fixture builders for the broker-v2 panel tests (S1)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.budget_projection import BotResult
from app.broker.alpaca.clerk.sqlite.decision_receipts import DecisionOutcome, DecisionReceipt
from app.engine.live.order_identity import NAMESPACE_ROOT
from app.schemas.account_authority import AuthorityKind
from app.services.broker_v2_panel.sqlite_panel_adapter import CatalogHomeFacts

ACCT = "test-acct"
SID = "bot-alpha"
OTHER_SID = "bot-beta"


def home_facts(
    *,
    world: AuthorityKind = "real_paper",
    holding: Sequence[str] = (),
    latest_stops: Mapping[str, int | None] | None = None,
) -> CatalogHomeFacts:
    """Home facts for a built catalog; no bot has stopped a run unless named."""
    return CatalogHomeFacts(world=world, holding_money=frozenset(holding), latest_stops=dict(latest_stops or {}))


def read_results_from(results: Mapping[str, BotResult] | None) -> Callable[[Sequence[str]], dict[str, BotResult]]:
    """A Finished-results read; with no ``results`` the fee evidence is unknown."""

    def read_results(sids: Sequence[str]) -> dict[str, BotResult]:
        if results is None:
            raise BudgetUnavailable("Fee evidence is unresolved: no evidence in this fixture")
        return {sid: results[sid] for sid in sids}

    return read_results


def order_ref(sid: str, intent: str) -> str:
    return f"{NAMESPACE_ROOT}/{sid}/v1:{intent}"


def decision_receipt(
    *,
    seq: int,
    ts_ms: int,
    outcome: DecisionOutcome,
    reason_code: str,
    bar_ref: str = "bar-1",
    order_ref: str = "",
    intent_id: str = "",
    decision_id: str | None = None,
    effect_operation_id: str | None = None,
) -> DecisionReceipt:
    return DecisionReceipt(
        seq=seq,
        ts_ms=ts_ms,
        bar_ref=bar_ref,
        outcome=outcome,
        reason_code=reason_code,
        order_ref=order_ref,
        intent_id=intent_id,
        decision_id=decision_id,
        effect_operation_id=effect_operation_id,
    )
