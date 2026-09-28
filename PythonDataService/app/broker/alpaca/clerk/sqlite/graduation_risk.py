"""Reviewed Live risk policy carried by the existing durable activation proof.

The target stays empty until activation. Before any Live runtime is composed,
its verified activation completes the one initial policy transition. Restart
repeats this step safely; subsequent explicit policy edits are never reset.
Shadow holds, budgets, positions and session equity are never read or copied.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import shadow_account_id_for_live_account
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord, ActivationRecordInvalid
from app.broker.alpaca.clerk.sqlite.operational_files import confined_relative_path
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


@dataclass(frozen=True)
class GraduationRiskReview:
    source_account_id: str
    source_policy: AccountRiskPolicy
    selection_generation: int
    profile_id: str
    profile_revision: int
    actor: str
    extended_hours_entry_bps: float
    extended_hours_exit_bps: float

    @classmethod
    def from_payload(cls, payload: dict) -> GraduationRiskReview:
        return cls(**{**payload, "source_policy": AccountRiskPolicy(**payload["source_policy"])})


def initialize_reviewed_live_risk(
    repo: ClerkSqliteRepository, *, activation: ActivationRecord, artifacts_root: Path,
) -> None:
    """Finish the reviewed activation before installing any mutation capability."""
    path = confined_relative_path(artifacts_root, activation.broker_proof_reference)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != activation.broker_proof_sha256:
        raise ActivationRecordInvalid("graduation risk proof hash does not verify")
    payload = json.loads(raw)
    review_payload = payload.get("graduation_risk")
    if review_payload is None:
        return  # Historical activation: no new policy consent is invented.
    review = GraduationRiskReview.from_payload(review_payload)
    if payload.get("account_id") != repo.account_id or review.source_account_id != shadow_account_id_for_live_account(repo.account_id):
        raise ActivationRecordInvalid("graduation risk review names a different account")
    with repo._write_lock:
        if repo.account_risk_policy() is not None:
            return  # The first transition or a later explicit Apply already won.
        if any(repo._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in ("runs", "orders", "fills", "deployment_budgets")):
            raise ActivationRecordInvalid("reviewed Live policy is missing after custody began")
        append_risk_policy(repo, policy=AccountRiskPolicy(
            revision=1, loss_fraction=review.source_policy.loss_fraction,
            loss_usd=review.source_policy.loss_usd, profile_id=review.profile_id,
            profile_revision=review.profile_revision, actor=review.actor,
            applied_at_ms=activation.activated_at_ms,
        ), expected_revision=0)
