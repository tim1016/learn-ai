"""Notice-shaped post-mutation rung receipts (ADR-0015 / ADR-0025, PRD #972).

Authors the ``MutationRungReceipt`` returned by Bot Control mutations from a
fresh ``LiveInstanceStatus``: the current blockage-ladder rung decides the
receipt, and account-truth attention groups become observational warnings when
no rung blocks. Routers resolve the fresh status; this module owns the copy
and the actionability pairing.
"""

from __future__ import annotations

from typing import Literal

from app.operator.notices.schema import OperatorNoticeAction
from app.schemas.live_runs import LiveInstanceStatus, MutationRungReceipt

# Closed operator-facing labels per mutation key. Keys cover every mutation
# endpoint and every CommandVerb value; direct indexing keeps a missing label
# loud instead of mislabeling a receipt.
MUTATION_LABELS: dict[str, str] = {
    "start": "Start",
    "stop": "Stop",
    "pause": "Pause",
    "resume": "Resume",
    "reconcile": "Reconcile",
    "flatten": "Flatten",
    "flatten_and_pause": "Flatten-and-pause",
    "mark_poisoned": "Mark Poisoned",
    "crash_recovery_override": "Crash-recovery override",
    "emergency_flatten": "Emergency flatten",
}


def _stage_tier(severity: str) -> Literal["info", "warning", "critical"]:
    if severity == "critical":
        return "critical"
    if severity == "warning":
        return "warning"
    return "info"


def _receipt_none_action() -> OperatorNoticeAction:
    return OperatorNoticeAction(kind="none")


def _receipt_for_current_stage(
    status: LiveInstanceStatus,
    *,
    mutation_key: str,
) -> MutationRungReceipt:
    mutation_label = MUTATION_LABELS[mutation_key]
    surface = status.operator_surface
    current = next((stage for stage in surface.blockage_ladder.stages if stage.current), None)
    now_ms = status.fetched_at_ms
    if current is None:
        return MutationRungReceipt(
            code="mutation.scoped_all_clear",
            tier="info",
            title=f"{mutation_label} accepted. No enforced gate blocks the next start.",
            message=(
                "The fresh blockage ladder has no current rung. This only covers enforced start and "
                "submit gates; keep broker/account observations visible while the bot restarts."
            ),
            rung_id=None,
            actionability="self_resolving",
            resolution="No further enforced gate resolution is required for the next start attempt.",
            action=_receipt_none_action(),
            occurred_at_ms=now_ms,
        )

    reason_codes = list(current.reason_codes)
    if "CRASH_RECOVERY_REQUIRED" in reason_codes:
        if mutation_key == "resume":
            title = (
                "Stop latch cleared. The bot still won't run: previous host runner crashed "
                "— record crash-recovery evidence"
            )
            message = (
                "Resume persisted desired_state=RUNNING, but Start remains blocked until audited "
                "recovery evidence is recorded for this bot and broker account."
            )
        else:
            title = f"{mutation_label} accepted. Previous host runner crashed."
            message = current.summary
        return MutationRungReceipt(
            code="mutation.next_blocking_rung",
            tier="critical",
            title=title,
            message=message,
            rung_id=current.id,
            source_codes=reason_codes,
            actionability="actuatable",
            resolution="Clears when audited crash-recovery evidence is recorded for this account and bot.",
            action=OperatorNoticeAction(
                kind="focus_cockpit_action",
                label="Record recovery override",
                target="crash_recovery_override",
            ),
            occurred_at_ms=now_ms,
        )

    if current.id == "host_process" and surface.host_process.start_capability.enabled:
        return MutationRungReceipt(
            code="mutation.next_blocking_rung",
            tier=_stage_tier(current.severity),
            title=f"{mutation_label} accepted. Next rung: start the bot process.",
            message=current.summary,
            rung_id=current.id,
            source_codes=reason_codes,
            actionability="actuatable",
            resolution="Clears when the host daemon reports this bot process is running.",
            action=OperatorNoticeAction(
                kind="focus_cockpit_action",
                label="Start bot process",
                target="start_process",
            ),
            occurred_at_ms=now_ms,
        )

    if current.id == "reconciliation" and surface.host_process.state == "RUNNING":
        return MutationRungReceipt(
            code="mutation.next_blocking_rung",
            tier=_stage_tier(current.severity),
            title=f"{mutation_label} accepted. Next rung: reconciliation.",
            message=current.summary,
            rung_id=current.id,
            source_codes=reason_codes,
            actionability="actuatable",
            resolution="Clears when runtime reconciliation reaches a clean or adopted receipt.",
            action=OperatorNoticeAction(
                kind="focus_cockpit_action",
                label="Reconcile now",
                target="reconcile_now",
            ),
            occurred_at_ms=now_ms,
        )

    routed_targets: dict[str, tuple[str, str, str]] = {
        "control_plane": (
            "Check host daemon",
            "host_daemon",
            "Clears when the data plane can prove host-daemon connectivity again.",
        ),
        "broker": (
            "Check IBKR session",
            "ibkr_connection",
            "Clears when broker safety, connection, and submit capability evidence are proven.",
        ),
        "account_safety": (
            "Review account safety",
            "account_safety",
            "Clears when account safety evidence is proven clean or the blocking account condition is resolved.",
        ),
        "account_owner": (
            "Review AccountOwner",
            "account_owner",
            "Clears when AccountOwner generation and accepting phase are proven.",
        ),
        "preflight": (
            "Review pre-flight gate",
            "preflight",
            "Clears when the named pre-flight gate passes.",
        ),
    }
    routed = routed_targets.get(current.id)
    if routed is not None:
        label, target, resolution = routed
        return MutationRungReceipt(
            code="mutation.next_blocking_rung",
            tier=_stage_tier(current.severity),
            title=f"{mutation_label} accepted. Next rung: {current.label}.",
            message=f"{current.title}. {current.summary}",
            rung_id=current.id,
            source_codes=reason_codes,
            actionability="routed",
            resolution=current.next_step or resolution,
            action=OperatorNoticeAction(
                kind="external_manual_check",
                label=label,
                target=target,
            ),
            occurred_at_ms=now_ms,
        )

    return MutationRungReceipt(
        code="mutation.next_blocking_rung",
        tier=_stage_tier(current.severity),
        title=f"{mutation_label} accepted. Next rung: {current.label}.",
        message=f"{current.title}. {current.summary}",
        rung_id=current.id,
        source_codes=reason_codes,
        actionability="self_resolving",
        resolution=current.next_step or "Clears when the fresh operator surface no longer marks this rung current.",
        action=_receipt_none_action(),
        occurred_at_ms=now_ms,
    )


def _observational_receipt_warnings(status: LiveInstanceStatus) -> list[MutationRungReceipt]:
    warnings: list[MutationRungReceipt] = []
    now_ms = status.fetched_at_ms
    for group in status.operator_surface.trader_guidance.additional_attention_groups:
        if group.code != "account_truth":
            continue
        warnings.append(
            MutationRungReceipt(
                code="mutation.observational_warning",
                tier=group.severity,
                title=group.headline,
                message=group.explanation,
                rung_id="account_safety",
                source_codes=[group.code],
                actionability="routed",
                resolution=group.operator_next_step,
                action=OperatorNoticeAction(
                    kind="external_manual_check",
                    label="Review Account Truth",
                    target="account_truth",
                ),
                occurred_at_ms=now_ms,
            )
        )
    return warnings


def mutation_rung_receipts(
    status: LiveInstanceStatus,
    *,
    mutation_key: str,
) -> tuple[MutationRungReceipt, list[MutationRungReceipt]]:
    """Author the receipt and observational warnings for a completed mutation."""
    receipt = _receipt_for_current_stage(status, mutation_key=mutation_key)
    warnings = _observational_receipt_warnings(status) if receipt.code == "mutation.scoped_all_clear" else []
    return receipt, warnings
