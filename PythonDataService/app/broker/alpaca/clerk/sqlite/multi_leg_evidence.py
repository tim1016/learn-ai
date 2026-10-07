"""Fold one option parent's answer onto its own row and each leg's row (ADR 0076)."""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.option_order_evidence import (
    MultiLegOrderEvidence,
    asked_option_order,
    evidence_of_the_order_asked,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.options import BrokerOptionOrder


def fold_multi_leg_order_evidence(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    evidence: MultiLegOrderEvidence,
    order_ref: str | None = None,
    simulated_authority: bool = False,
    append_stale_ack: bool = True,
    acknowledgement_only: bool = False,
) -> None:
    """Keep every leg's evidence before the parent's state; a parent never has a fill.

    A real submit answer acknowledges only. An exact lookup recovers each
    leg's cumulative fills, and a simulation provides exact executions.
    Repeated or reordered answers use the existing per-order monotone folds.
    """
    from app.broker.alpaca.clerk.sqlite.order_evidence import (
        fence_fills_on_terminal_enters,
        fold_enter_unfilled_if_proven,
        fold_order_acknowledgement,
        fold_order_evidence,
        fold_uncertain,
    )

    parent_ref = order_ref or evidence.client_order_id
    if parent_ref is None:
        raise ValueError("a multi-leg answer needs its parent's captured reference")
    with repo.write_fence():
        captured = {leg.symbol: leg for leg in repo.order_legs(parent_ref)}
        valid = (
            bool(evidence.order_id.strip()) and bool(evidence.status.strip())
            and not evidence.unreadable_fields
            and len(evidence.legs) == len(captured)
            and {leg.symbol for leg in evidence.legs} == captured.keys()
            and all(leg.side == captured[leg.symbol].side for leg in evidence.legs)
        )
        if not valid:
            fold_uncertain(
                repo, effect_operation_id=effect_operation_id, order_ref=parent_ref,
                why="The broker's multi-leg answer does not name the whole captured order and each leg's side.",
            )
            return
        incomplete = False
        for observed in evidence.legs:
            leg = captured[observed.symbol]
            unreadable = (
                not observed.order_id.strip() or not observed.status.strip() or bool(observed.unreadable_fields)
                or (observed.filled_quantity > 0 and observed.filled_avg_price is None)
            )
            if unreadable:
                incomplete = True
                continue
            if acknowledgement_only:
                fold_order_acknowledgement(
                    repo, effect_operation_id=effect_operation_id, order=observed,
                    order_ref=leg.order_ref, append_stale_ack=append_stale_ack,
                )
            else:
                fold_order_evidence(
                    repo, effect_operation_id=effect_operation_id, order=observed,
                    order_ref=leg.order_ref, simulated_authority=simulated_authority,
                    append_stale_ack=append_stale_ack,
                )
        if incomplete:
            fold_uncertain(
                repo, effect_operation_id=effect_operation_id, order_ref=parent_ref,
                why="The broker counted a leg without readable identity, state or fill price; the entry remains unknown.",
            )
            return
        fold_order_acknowledgement(
            repo, effect_operation_id=effect_operation_id, order=evidence,
            order_ref=parent_ref, append_stale_ack=append_stale_ack,
        )
        if not acknowledgement_only and not simulated_authority:
            fold_enter_unfilled_if_proven(
                repo, effect_operation_id=effect_operation_id, order=evidence, order_ref=parent_ref,
            )
        parent = repo.order(parent_ref)
        if parent is not None and parent.role == "ENTRY":
            from app.broker.alpaca.clerk.sqlite.legs_out_of_step import note_legs_out_of_step

            fence_fills_on_terminal_enters(repo, order_ref=parent_ref)
            note_legs_out_of_step(repo, parent_ref)


def fold_multi_leg_answer(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    parent_order_ref: str,
    answer: BrokerOptionOrder,
    simulated_authority: bool = False,
) -> None:
    """Hold a raw option answer to the captured order, then fold its legs."""
    asked = asked_option_order(repo, parent_order_ref)
    if asked is None:
        raise ValueError(f"{parent_order_ref!r} is not a captured option order")
    evidence = evidence_of_the_order_asked(answer, asked=asked)
    if not isinstance(evidence, MultiLegOrderEvidence):
        raise ValueError(f"{parent_order_ref!r} is not a multi-leg parent")
    fold_multi_leg_order_evidence(
        repo, effect_operation_id=effect_operation_id, evidence=evidence,
        order_ref=parent_order_ref, simulated_authority=simulated_authority,
    )
