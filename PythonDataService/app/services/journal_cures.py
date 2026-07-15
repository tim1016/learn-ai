"""Authenticated operator cures for stale Clerk journal claims."""

from __future__ import annotations

from pathlib import Path

from app.engine.live.account_clerk_journal import (
    AccountClerkJournal,
    AccountClerkJournalEntry,
    AccountClerkOperatorAdjustment,
    AccountClerkOperatorAdjustmentConflict,
    read_account_clerk_journal,
)
from app.engine.live.journal_exposure import project_journal_exposure
from app.schemas.journal_cures import JournalCurePreview, JournalCureReceipt, JournalCureRequest
from app.utils.timestamps import now_ms_utc


class JournalCureError(ValueError):
    """A cure request that cannot safely change the explained claim ledger."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


class JournalCureService:
    """Append-only correction service; broker truth is deliberately out of scope."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def apply(
        self,
        *,
        account_id: str,
        request: JournalCureRequest,
        now_ms: int | None = None,
    ) -> JournalCureReceipt:
        """Reduce a durable namespace claim or return its idempotent prior receipt."""

        journal = AccountClerkJournal(artifacts_root=self._artifacts_root, account_id=account_id)
        adjustment = AccountClerkOperatorAdjustment(
            account_id=account_id,
            bot_order_namespace=request.bot_order_namespace,
            symbol=request.symbol,
            signed_quantity=request.signed_quantity,
            request_provenance=request.request_provenance,
            reason=request.reason,
            evidence_refs=request.evidence_refs,
            idempotency_key=request.idempotency_key,
            recorded_at_ms=now_ms_utc() if now_ms is None else now_ms,
        )
        try:
            entry = journal.append_operator_adjustment(
                adjustment,
                validate_adjustment=lambda entries: _validate_claim_reduction(
                    entries,
                    account_id=account_id,
                    request=request,
                ),
            )
        except AccountClerkOperatorAdjustmentConflict as exc:
            raise JournalCureError(
                "JOURNAL_CURE_IDEMPOTENCY_CONFLICT",
                "idempotency key was already used with a different cure payload",
            ) from exc
        return _receipt(entry)

    def preview(
        self,
        *,
        account_id: str,
        bot_order_namespace: str,
        symbol: str,
    ) -> JournalCurePreview:
        """Return the journal claim that constrains a future immutable cure."""

        normalized_symbol = symbol.strip().upper()
        quantity = _namespace_quantity(
            read_account_clerk_journal(self._artifacts_root, account_id),
            account_id=account_id,
            namespace=bot_order_namespace,
            symbol=normalized_symbol,
        )
        if quantity == 0:
            return JournalCurePreview(
                account_id=account_id,
                bot_order_namespace=bot_order_namespace,
                symbol=normalized_symbol,
                journal_quantity=0.0,
                can_cure=False,
                reason_code="JOURNAL_CURE_NO_STALE_CLAIM",
            )
        return JournalCurePreview(
            account_id=account_id,
            bot_order_namespace=bot_order_namespace,
            symbol=normalized_symbol,
            journal_quantity=quantity,
            required_adjustment_sign="negative" if quantity > 0 else "positive",
            can_cure=True,
            reason_code="JOURNAL_CURE_CLAIM_REDUCIBLE",
        )


def _namespace_quantity(
    entries: list[AccountClerkJournalEntry],
    *,
    account_id: str,
    namespace: str,
    symbol: str,
) -> float:
    return next(
        (
            exposure.quantity
            for exposure in project_journal_exposure(entries, account_id=account_id, group_by="namespace")
            if exposure.group_id == namespace and exposure.symbol == symbol
        ),
        0.0,
    )


def _validate_claim_reduction(
    entries: list[AccountClerkJournalEntry],
    *,
    account_id: str,
    request: JournalCureRequest,
) -> None:
    """Reject a new cure unless the locked journal proves it reduces a claim."""

    current_quantity = _namespace_quantity(
        entries,
        account_id=account_id,
        namespace=request.bot_order_namespace,
        symbol=request.symbol,
    )
    if current_quantity == 0:
        raise JournalCureError(
            "JOURNAL_CURE_NO_STALE_CLAIM",
            "namespace has no Clerk-attributed claim for this symbol to cure",
        )
    if current_quantity * request.signed_quantity >= 0 or abs(request.signed_quantity) > abs(current_quantity):
        raise JournalCureError(
            "JOURNAL_CURE_MUST_REDUCE_CLAIM",
            "cure must only reduce the existing namespace claim and may not cross zero",
        )


def _receipt(entry: AccountClerkJournalEntry) -> JournalCureReceipt:
    assert entry.operator_adjustment is not None
    return JournalCureReceipt(
        **entry.operator_adjustment.model_dump(),
        journal_seq=entry.seq,
    )
