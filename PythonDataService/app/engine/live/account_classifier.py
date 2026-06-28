"""Account-scoped classifier for lifecycle gate decisions.

Pure V1 classifier: consumes broker evidence, account instance registry rows,
durable submit intents, optional fleet baseline evidence, and optional audited
operator override evidence. It returns one explicit decision that can be
projected directly into the shared GateResult contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.engine.live.account_artifacts import (
    ACTIVE_INSTANCE_BINDING_STATES,
    AccountInstanceBinding,
)
from app.engine.live.order_identity import OrderRefParseError, parse_order_ref
from app.engine.live.reconciliation_classifier import BrokerSnapshot
from app.schemas.live_runs import GateResult

BrokerEvidenceStatus = Literal[
    "available",
    "retryable_unavailable",
    "unprovable",
    "unknown",
]

AccountClassifierOutcome = Literal[
    "continue",
    "adopt",
    "pause",
    "ignore_baseline",
    "retry",
    "freeze",
    "poison_run",
    "unknown",
]


@dataclass(frozen=True)
class AccountBrokerEvidence:
    status: BrokerEvidenceStatus = "available"
    snapshot: BrokerSnapshot | None = None
    detail: str = ""


@dataclass(frozen=True)
class AccountDurableIntent:
    account_id: str
    strategy_instance_id: str
    run_id: str
    bot_order_namespace: str
    intent_id: str
    order_ref: str
    status: str
    recorded_at_ms: int


@dataclass(frozen=True)
class AccountBaselineEvidence:
    baseline_id: str
    cutoff_ms: int
    source: str


@dataclass(frozen=True)
class AccountOperatorOverride:
    override_id: str
    decision: Literal["retry", "freeze", "poison_run", "continue", "adopt", "ignore_baseline"]
    reason: str
    approved_at_ms: int
    approved_by: str
    account_id: str | None = None
    valid_until_ms: int | None = None
    prior_evidence: dict | None = None
    next_reconciliation_step: str | None = None
    strategy_instance_id: str | None = None
    run_id: str | None = None
    bot_order_namespace: str | None = None
    affected_order_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountClassifierDecision:
    outcome: AccountClassifierOutcome
    reason: str
    account_id: str
    decided_at_ms: int
    strategy_instance_id: str | None = None
    run_id: str | None = None
    bot_order_namespace: str | None = None
    affected_order_refs: tuple[str, ...] = ()
    baseline_id: str | None = None
    override_id: str | None = None

    def to_gate_result(self) -> GateResult:
        return GateResult(
            gate_id="account.classifier",
            status=_GATE_STATUS_BY_OUTCOME[self.outcome],
            source="account_classifier",
            operator_reason=self.reason,
            operator_next_step=_NEXT_STEP_BY_OUTCOME[self.outcome],
            evidence_at_ms=self.decided_at_ms,
        )


_GATE_STATUS_BY_OUTCOME: dict[AccountClassifierOutcome, str] = {
    "continue": "pass",
    "ignore_baseline": "pass",
    "adopt": "block",
    "pause": "freeze",
    "retry": "unknown",
    "freeze": "freeze",
    "poison_run": "poison",
    "unknown": "freeze",
}

_NEXT_STEP_BY_OUTCOME: dict[AccountClassifierOutcome, str] = {
    "continue": "GATE_PASSING",
    "ignore_baseline": "GATE_PASSING",
    "adopt": "ADOPT_BROKER_EVIDENCE",
    "pause": "OPERATOR_RESUME_REQUIRED",
    "retry": "RETRY_BROKER_SNAPSHOT",
    "freeze": "CHECK_IBKR",
    "poison_run": "REDEPLOY_OR_RECOVER",
    "unknown": "CHECK_BROKER_EVIDENCE",
}


def classify_account(
    *,
    account_id: str,
    broker: AccountBrokerEvidence,
    registry_bindings: tuple[AccountInstanceBinding, ...],
    durable_intents: tuple[AccountDurableIntent, ...],
    baseline: AccountBaselineEvidence | None,
    operator_override: AccountOperatorOverride | None,
    now_ms: int,
) -> AccountClassifierDecision:
    override_rejection = _reject_invalid_override(account_id, operator_override, now_ms)
    if override_rejection is not None:
        return override_rejection
    active_override = operator_override
    if broker.status == "retryable_unavailable":
        return _decision(
            "retry",
            "BROKER_STATE_RETRYABLE",
            account_id=account_id,
            now_ms=now_ms,
            override=active_override if active_override and active_override.decision == "retry" else None,
        )
    if broker.status == "unprovable":
        if active_override is not None and active_override.decision == "continue":
            return _decision(
                "continue",
                "OPERATOR_OVERRIDE_CONTINUE",
                account_id=account_id,
                now_ms=now_ms,
                override=active_override,
            )
        return _decision(
            "freeze",
            "BROKER_STATE_UNPROVABLE",
            account_id=account_id,
            now_ms=now_ms,
        )
    if broker.status == "unknown":
        if active_override is not None and active_override.decision == "continue":
            return _decision(
                "continue",
                "OPERATOR_OVERRIDE_CONTINUE",
                account_id=account_id,
                now_ms=now_ms,
                override=active_override,
            )
        return _decision(
            "unknown",
            "BROKER_STATE_UNKNOWN",
            account_id=account_id,
            now_ms=now_ms,
        )

    snapshot = broker.snapshot or BrokerSnapshot()
    namespace_bindings = _active_bindings_by_namespace(registry_bindings, account_id)
    if _has_duplicate_active_namespace(registry_bindings, account_id):
        return _decision(
            "freeze",
            "ACCOUNT_REGISTRY_DUPLICATE_NAMESPACE",
            account_id=account_id,
            now_ms=now_ms,
        )

    intents_by_ref = {intent.order_ref: intent for intent in durable_intents if intent.account_id == account_id}
    adopt_refs: list[str] = []
    baseline_refs: list[str] = []
    affected_binding: AccountInstanceBinding | None = None

    def consider(order_ref: str | None, *, active: bool, exec_time_ms: int | None) -> AccountClassifierDecision | None:
        nonlocal affected_binding
        if order_ref is None:
            affected_binding = affected_binding or _single_active_binding(namespace_bindings)
            if active_override is not None and active_override.decision == "continue":
                return _decision(
                    "freeze",
                    "OPERATOR_OVERRIDE_CONTRADICTED",
                    account_id=account_id,
                    now_ms=now_ms,
                    binding=affected_binding,
                    override=active_override,
                )
            return _decision(
                "poison_run",
                "NO_ORDER_REF",
                account_id=account_id,
                now_ms=now_ms,
                binding=affected_binding,
                override=active_override if active_override and active_override.decision == "poison_run" else None,
            )
        try:
            namespace, _intent_id = parse_order_ref(order_ref)
        except OrderRefParseError:
            affected_binding = affected_binding or _single_active_binding(namespace_bindings)
            if active_override is not None and active_override.decision == "continue":
                return _decision(
                    "freeze",
                    "OPERATOR_OVERRIDE_CONTRADICTED",
                    account_id=account_id,
                    now_ms=now_ms,
                    binding=affected_binding,
                    affected_order_refs=(order_ref,),
                    override=active_override,
                )
            return _decision(
                "poison_run",
                "UNPARSEABLE_ORDER_REF",
                account_id=account_id,
                now_ms=now_ms,
                binding=affected_binding,
                affected_order_refs=(order_ref,),
                override=active_override if active_override and active_override.decision == "poison_run" else None,
            )

        binding = namespace_bindings.get(namespace)
        if binding is None:
            if not active and baseline is not None and exec_time_ms is not None and exec_time_ms <= baseline.cutoff_ms:
                baseline_refs.append(order_ref)
                return None
            if active_override is not None and active_override.decision == "continue":
                return _decision(
                    "freeze",
                    "OPERATOR_OVERRIDE_CONTRADICTED",
                    account_id=account_id,
                    now_ms=now_ms,
                    affected_order_refs=(order_ref,),
                    override=active_override,
                )
            return _decision(
                "poison_run",
                "UNKNOWN_NAMESPACE",
                account_id=account_id,
                now_ms=now_ms,
                affected_order_refs=(order_ref,),
                override=active_override if active_override and active_override.decision == "poison_run" else None,
            )

        affected_binding = affected_binding or binding
        if order_ref in intents_by_ref:
            return None
        adopt_refs.append(order_ref)
        return None

    for order in snapshot.open_orders:
        decision = consider(
            order.order_ref,
            active=_is_active_order(order),
            exec_time_ms=None,
        )
        if decision is not None:
            return decision
    for execution in snapshot.executions:
        decision = consider(
            execution.order_ref,
            active=False,
            exec_time_ms=execution.exec_time_ms,
        )
        if decision is not None:
            return decision

    if adopt_refs:
        if active_override is not None and active_override.decision == "continue":
            return _decision(
                "freeze",
                "OPERATOR_OVERRIDE_CONTRADICTED",
                account_id=account_id,
                now_ms=now_ms,
                binding=affected_binding,
                affected_order_refs=tuple(adopt_refs),
                override=active_override,
            )
        return _decision(
            "adopt",
            "REGISTERED_NAMESPACE_BROKER_ORPHAN",
            account_id=account_id,
            now_ms=now_ms,
            binding=affected_binding,
            affected_order_refs=tuple(adopt_refs),
            override=active_override if active_override and active_override.decision == "adopt" else None,
        )
    if baseline_refs and baseline is not None:
        return _decision(
            "ignore_baseline",
            "BASELINE_COVERED_HISTORICAL_EXECUTION",
            account_id=account_id,
            now_ms=now_ms,
            binding=affected_binding,
            affected_order_refs=tuple(baseline_refs),
            baseline=baseline,
            override=active_override if active_override and active_override.decision == "ignore_baseline" else None,
        )
    return _decision(
        "continue",
        "OPERATOR_OVERRIDE_CONTINUE"
        if active_override is not None and active_override.decision == "continue"
        else "ACCOUNT_STATE_MATCHES_REGISTRY",
        account_id=account_id,
        now_ms=now_ms,
        binding=affected_binding or _single_active_binding(namespace_bindings),
        override=active_override if active_override and active_override.decision == "continue" else None,
    )


def _decision(
    outcome: AccountClassifierOutcome,
    reason: str,
    *,
    account_id: str,
    now_ms: int,
    binding: AccountInstanceBinding | None = None,
    affected_order_refs: tuple[str, ...] = (),
    baseline: AccountBaselineEvidence | None = None,
    override: AccountOperatorOverride | None = None,
) -> AccountClassifierDecision:
    return AccountClassifierDecision(
        outcome=outcome,
        reason=reason,
        account_id=account_id,
        decided_at_ms=now_ms,
        strategy_instance_id=binding.strategy_instance_id if binding is not None else None,
        run_id=binding.run_id if binding is not None else None,
        bot_order_namespace=binding.bot_order_namespace if binding is not None else None,
        affected_order_refs=affected_order_refs,
        baseline_id=baseline.baseline_id if baseline is not None else None,
        override_id=override.override_id if override is not None else None,
    )


def _reject_invalid_override(
    account_id: str,
    override: AccountOperatorOverride | None,
    now_ms: int,
) -> AccountClassifierDecision | None:
    if override is None:
        return None
    if override.account_id is not None and override.account_id != account_id:
        return _decision(
            "freeze",
            "OPERATOR_OVERRIDE_ACCOUNT_MISMATCH",
            account_id=account_id,
            now_ms=now_ms,
            override=override,
        )
    if override.valid_until_ms is not None and override.valid_until_ms < now_ms:
        return _decision(
            "freeze",
            "OPERATOR_OVERRIDE_STALE",
            account_id=account_id,
            now_ms=now_ms,
            override=override,
        )
    return None


def _active_bindings_by_namespace(
    bindings: tuple[AccountInstanceBinding, ...],
    account_id: str,
) -> dict[str, AccountInstanceBinding]:
    latest_by_instance: dict[str, AccountInstanceBinding] = {}
    for binding in bindings:
        if binding.account_id == account_id:
            latest_by_instance[binding.strategy_instance_id] = binding
    return {
        binding.bot_order_namespace: binding
        for binding in latest_by_instance.values()
        if binding.lifecycle_state in ACTIVE_INSTANCE_BINDING_STATES
    }


def _has_duplicate_active_namespace(
    bindings: tuple[AccountInstanceBinding, ...],
    account_id: str,
) -> bool:
    namespaces: set[str] = set()
    for binding in bindings:
        if binding.account_id == account_id and binding.lifecycle_state in ACTIVE_INSTANCE_BINDING_STATES:
            if binding.bot_order_namespace in namespaces:
                return True
            namespaces.add(binding.bot_order_namespace)
    return False


def _single_active_binding(
    namespace_bindings: dict[str, AccountInstanceBinding],
) -> AccountInstanceBinding | None:
    if len(namespace_bindings) != 1:
        return None
    return next(iter(namespace_bindings.values()))


def _is_active_order(order) -> bool:
    return order.status in {"PendingSubmit", "PreSubmitted", "Submitted", "ApiPending"} or order.remaining > 0.0


__all__ = [
    "AccountBaselineEvidence",
    "AccountBrokerEvidence",
    "AccountClassifierDecision",
    "AccountClassifierOutcome",
    "AccountDurableIntent",
    "AccountOperatorOverride",
    "classify_account",
]
