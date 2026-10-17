"""Verify an Options bot's exact golden setup without constructing a Signal Program (ADR 0079)."""

from __future__ import annotations

import logging

from app.schemas.run_admission import ProgramBuildAdmissionFact, StrategyValidationAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding

logger = logging.getLogger(__name__)


async def options_start_facts(
    binding: BrokerBotBinding, *, verified_at_ms: int
) -> tuple[ProgramBuildAdmissionFact, StrategyValidationAdmissionFact]:
    """Re-read approval for Start/Resume; unavailable evidence never authorizes new exposure."""
    from app.options_bots.golden import GoldenSetupError, get_ready_setup

    configuration = binding.options_bot
    if configuration is None:
        raise ValueError("Options admission requires an approved setup configuration.")
    detail: str | None = None
    try:
        golden = await get_ready_setup(configuration.golden_setup_id)
        if (
            golden.setup_sha256 != configuration.setup_sha256
            or golden.setup != configuration.setup
        ):
            detail = "The deployed Options setup differs from the exact approved setup or its proven rules."
    except GoldenSetupError as exc:
        detail = str(exc)
    except Exception:
        logger.exception("Options setup approval could not be verified", extra={"action": "options_approval_unavailable"})
        detail = "The golden setup approval is unavailable. Restore its evidence before opening positions."
    references = (f"golden-option-setup:{configuration.golden_setup_id}",)
    if detail is not None:
        return (
            ProgramBuildAdmissionFact(
                state="UNPROVEN", program_key=binding.strategy_key, verified_at_ms=verified_at_ms,
                evidence_refs=references, explanation=detail, next_step="Review the golden setup qualification.",
            ),
            StrategyValidationAdmissionFact(
                state="UNVERIFIED", strategy_key=binding.strategy_key, evidence_status="blocked",
                verified_at_ms=verified_at_ms, evidence_refs=references, explanation=detail,
            ),
        )
    return (
        ProgramBuildAdmissionFact(
            state="PROVEN", program_key=binding.strategy_key, program_version=golden.rules_digest,
            golden_trace_root=golden.proof["result_sha256"], running_artifact_digest=golden.rules_digest,
            qualification_receipt_hash=golden.proof_sha256, verified_at_ms=verified_at_ms,
            evidence_refs=references, corpus_coverage="COVERED", wiring="MATCHED",
            explanation="The approved Options setup reproduces its recorded outcomes under these rule bytes.",
        ),
        StrategyValidationAdmissionFact(
            state="VERIFIED", strategy_key=binding.strategy_key, evidence_status="accepted",
            event_id=golden.id, evidence_snapshot_sha256=golden.proof_sha256,
            verified_at_ms=verified_at_ms, evidence_refs=references,
            explanation="The owner approved this exact tested Options setup; its recorded limitations still apply.",
        ),
    )
