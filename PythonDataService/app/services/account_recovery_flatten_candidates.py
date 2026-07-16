"""Server-authored, conservative recovery-flatten candidates for Account desk."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_artifacts import read_account_owner_generation
from app.engine.live.account_clerk import AccountClerkJournalCorruptError, read_account_clerk_journal
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import AccountInstanceBinding
from app.engine.live.journal_exposure import normalize_journal_broker_event, project_journal_exposure
from app.schemas.journal_cures import AccountRecoveryFlattenCandidate
from app.schemas.operator_blocker import OperatorConfirmationCopy


def recovery_flatten_candidates(
    *,
    artifacts_root: Path,
    account_id: str,
    bindings: list[AccountInstanceBinding],
    generated_at_ms: int,
) -> list[AccountRecoveryFlattenCandidate]:
    """Author only exact, single-instrument Clerk recovery requests.

    A Desk client never constructs an order from a condition or reason code.
    This deliberately conservative projection offers a move only when the
    durable journal identifies one retired namespace, one non-zero instrument,
    and one source order specification. The Clerk revalidates all of those
    facts at the mutation boundary.
    """

    owner_generation = read_account_owner_generation(artifacts_root, account_id)
    if owner_generation is None or owner_generation.phase != "accepting":
        return []
    try:
        entries = read_account_clerk_journal(artifacts_root, account_id)
    except AccountClerkJournalCorruptError:
        return []

    source_intents: dict[tuple[str, str], dict[str, AccountOwnerSubmitIntent]] = {}
    for entry in entries:
        event = normalize_journal_broker_event(entry)
        intent = entry.intent
        if (
            event is None
            or intent is None
            or event.event_type != "fill"
            or event.symbol is None
        ):
            continue
        source_intents.setdefault(
            (intent.bot_order_namespace, event.symbol.upper()),
            {},
        )[intent.order_ref] = intent

    latest_by_namespace = {binding.bot_order_namespace: binding for binding in bindings}
    candidates: list[AccountRecoveryFlattenCandidate] = []
    for exposure in project_journal_exposure(entries, account_id=account_id, group_by="namespace"):
        binding = latest_by_namespace.get(exposure.group_id)
        sources = source_intents.get((exposure.group_id, exposure.symbol), {})
        if binding is None or binding.lifecycle_state != "RETIRED" or len(sources) != 1:
            continue
        source_intent = next(iter(sources.values()))
        candidate = _candidate_for_residual(
            account_id=account_id,
            binding=binding,
            source_intent=source_intent,
            symbol=exposure.symbol,
            signed_quantity=exposure.quantity,
            owner_generation=owner_generation.generation,
            generated_at_ms=generated_at_ms,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _candidate_for_residual(
    *,
    account_id: str,
    binding: AccountInstanceBinding,
    source_intent: AccountOwnerSubmitIntent,
    symbol: str,
    signed_quantity: float,
    owner_generation: int,
    generated_at_ms: int,
) -> AccountRecoveryFlattenCandidate | None:
    """Convert one journal-proven residual into its immutable Clerk intent."""

    if signed_quantity == 0 or not math.isfinite(signed_quantity):
        return None
    try:
        source_order = IbkrOrderSpec.model_validate(source_intent.order_spec)
    except ValidationError:
        return None
    if source_order.order_ref is None or source_order.symbol.upper() != symbol:
        return None
    digest_payload = json.dumps(
        {
            "account_id": account_id,
            "namespace": binding.bot_order_namespace,
            "source_order_ref": source_order.order_ref,
            "symbol": source_order.symbol,
            "signed_quantity": signed_quantity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(digest_payload).hexdigest()[:24]
    intent_id = f"account-desk-recovery-{digest}"
    order_ref = f"{binding.bot_order_namespace}:{intent_id}"
    recovery_order = source_order.model_copy(
        update={
            "action": "SELL" if signed_quantity > 0 else "BUY",
            "quantity": abs(signed_quantity),
            "order_type": "MKT",
            "limit_price": None,
            "time_in_force": "DAY",
            "confirm_paper": True,
            "client_order_id": f"account-desk-recovery-{digest}",
            "order_ref": order_ref,
        }
    )
    intent = AccountOwnerSubmitIntent(
        trace_id=f"account-desk-recovery:{digest}",
        account_id=account_id,
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        bot_order_namespace=binding.bot_order_namespace,
        intent_id=intent_id,
        order_ref=order_ref,
        intent_kind="RECOVERY_FLATTEN",
        order_spec=recovery_order.model_dump(mode="json"),
        owner_generation=owner_generation,
        created_at_ms=generated_at_ms,
    )
    return AccountRecoveryFlattenCandidate(
        intent=intent,
        confirmation=OperatorConfirmationCopy(
            title="Submit Clerk recovery flatten",
            body="Submit the server-projected recovery flatten for the retired bot namespace.",
            consequence="The Clerk will cancel namespace orders and submit the exact paper recovery order shown below.",
            confirm_label="Submit recovery flatten",
        ),
    )
