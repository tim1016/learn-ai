"""Hold one bot's entries when its confirmed option legs are out of step (ADR 0076)."""

from __future__ import annotations

import json

from app.broker.alpaca.clerk.sqlite.entry_outcome import entry_outcome
from app.broker.alpaca.clerk.sqlite.facts import UncertaintyRaisedFacts, UncertaintyResolvedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.option_entry import held_of_entry
from app.broker.alpaca.clerk.sqlite.reads import strategies_under_active_uncertainty
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    OPTION_LEGS_OUT_OF_STEP_REASON_CODE,
    OptionLegsOutOfStepCause,
)
from app.options.contract import OptionContract
from app.options.structure import ResolvedLeg, ResolvedStructure
from app.options.structure_check import StructureRefused, require_sold_legs_covered


def note_legs_out_of_step(repo: ClerkSqliteRepository, entry_order_ref: str) -> bool:
    """A final entry with uneven legs holds this bot alone, until its contracts are flat."""
    with repo.write_fence():
        if not repo.order_legs(entry_order_ref):
            return False
        outcome = entry_outcome(repo, entry_order_ref)
        if not outcome.proven or not outcome.legs_out_of_step:
            return False
        holding = held_of_entry(repo, entry_order_ref)
        if not any(leg.held for leg in holding.legs):
            return False
        parent = repo.order(entry_order_ref)
        assert parent is not None
        effect = repo.effect_operation(parent.effect_operation_id)
        assert effect is not None
        fill_counts = "entry_fill_counts:" + json.dumps(dict(outcome.contracts_filled), sort_keys=True, separators=(",", ":"))
        for episode in repo.uncertainty_history(
            scope="CUSTODY_SUBJECT", reason_code=OPTION_LEGS_OUT_OF_STEP_REASON_CODE,
            strategy_instance_id=effect.strategy_instance_id,
        ):
            previous = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"])
            if (
                episode["resolved_at_ms"] is not None
                and previous.cause_facts.get("entry_order_ref") == entry_order_ref
                and fill_counts in previous.evidence_refs
            ):
                return False
        cause = OptionLegsOutOfStepCause(
            entry_order_ref, tuple((leg.symbol, leg.held, leg.ratio, leg.position) for leg in holding.legs),
        )
        words = "; ".join(f"{leg.symbol}: {leg.held} confirmed contracts" for leg in holding.legs)
        confirmed = ResolvedStructure(legs=tuple(
            ResolvedLeg(
                leg_id=leg.leg_id, contract=OptionContract.from_occ_symbol(leg.symbol),
                position="long" if leg.held > 0 else "short", ratio=abs(leg.held),
            )
            for leg in holding.legs if leg.held
        ))
        try:
            require_sold_legs_covered(confirmed)
        except StructureRefused as refused:
            words += f". A sold contract is not covered: {refused}"
        result = raise_uncertainty(
            repo, strategy_instance_id=effect.strategy_instance_id,
            reason_code=OPTION_LEGS_OUT_OF_STEP_REASON_CODE,
            headline="The option legs filled out of step",
            explanation=words + ". The confirmed holdings do not make whole structures.",
            operator_impact="New entries are held for this bot. Its reductions and Flatten stay allowed; other bots continue.",
            next_step="Review the confirmed contracts and close what remains. The bot does not repair an uneven structure.",
            evidence_refs=(entry_order_ref, fill_counts), cause_facts=cause.to_mapping(), severity="error",
        )
        return result != "unchanged"


def bot_is_under_legs_out_of_step(repo: ClerkSqliteRepository, strategy_instance_id: str) -> bool:
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=OPTION_LEGS_OUT_OF_STEP_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    ) is not None


def end_notices_of_flat_bots(repo: ClerkSqliteRepository) -> tuple[str, ...]:
    """End a notice only after every contract it named is flat in this bot's custody."""
    ended = []
    with repo.write_fence() as conn:
        for sid in strategies_under_active_uncertainty(conn, OPTION_LEGS_OUT_OF_STEP_REASON_CODE):
            episode = repo.active_uncertainty(
                scope="CUSTODY_SUBJECT", reason_code=OPTION_LEGS_OUT_OF_STEP_REASON_CODE, strategy_instance_id=sid,
            )
            assert episode is not None
            facts = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"])
            cause = OptionLegsOutOfStepCause.from_mapping(facts.cause_facts)
            if any(repo.position(sid, symbol) != 0 for symbol, _held, _ratio, _position in cause.legs):
                continue

            def resolution(uncertainty_id: str, sid: str = sid) -> TransitionInput:
                return TransitionInput(
                    strategy_instance_id=sid, transition_kind="UNCERTAINTY_RESOLVED",
                    custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
                    clerk_observed_at_ms=repo.clock(), summary_code="OPTION_LEGS_OUT_OF_STEP_RESOLVED",
                    facts_json=UncertaintyResolvedFacts(
                        uncertainty_id=uncertainty_id, resolution_kind="ATTRIBUTED_FLAT_PROVEN",
                        evidence_refs=["attributed_positions"],
                    ).to_facts_json(),
                )

            if repo.resolve_uncertainty_if_active(
                scope="CUSTODY_SUBJECT", reason_code=OPTION_LEGS_OUT_OF_STEP_REASON_CODE,
                strategy_instance_id=sid, build_transition=resolution,
            ):
                ended.append(sid)
    return tuple(ended)


def refresh_legs_out_of_step_notices(repo: ClerkSqliteRepository) -> None:
    """Re-derive final-entry notices on each pass, recovering a crash after the fill's commit."""
    with repo.write_fence():
        for instance in repo.strategy_instances():
            for order in repo.entry_orders_for_strategy(instance["strategy_instance_id"]):
                note_legs_out_of_step(repo, order.order_ref)
        end_notices_of_flat_bots(repo)
