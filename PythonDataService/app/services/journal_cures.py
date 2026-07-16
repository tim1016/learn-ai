"""Authenticated operator cures for stale Clerk journal claims."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from app.engine.live.account_clerk_journal import (
    AccountClerkJournal,
    AccountClerkJournalEntry,
    AccountClerkOperatorAdjustment,
    AccountClerkOperatorAdjustmentConflict,
    read_account_clerk_journal,
)
from app.engine.live.account_registry import index_account_instance_bindings, read_account_instance_registry
from app.engine.live.journal_exposure import project_journal_exposure
from app.schemas.journal_cures import JournalCurePreview, JournalCureReceipt, JournalCureRequest
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.utils.timestamps import now_ms_utc


class JournalCureAppender(Protocol):
    """The Clerk-owned serialized append boundary required by a cure."""

    async def __call__(
        self,
        adjustment: AccountClerkOperatorAdjustment,
        *,
        validate_adjustment: Callable[[list[AccountClerkJournalEntry]], None],
    ) -> AccountClerkJournalEntry: ...


JournalCureHandler = Callable[[JournalCureRequest], Awaitable[JournalCureReceipt]]


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
        adjustment = self.adjustment_for(account_id=account_id, request=request, now_ms=now_ms)
        return self.apply_adjustment(account_id=account_id, adjustment=adjustment, request=request, journal=journal)

    def adjustment_for(
        self,
        *,
        account_id: str,
        request: JournalCureRequest,
        now_ms: int | None = None,
    ) -> AccountClerkOperatorAdjustment:
        """Build the immutable cure payload before handing it to the Clerk."""

        return AccountClerkOperatorAdjustment(
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

    def validate_adjustment(
        self,
        entries: list[AccountClerkJournalEntry],
        *,
        account_id: str,
        request: JournalCureRequest,
    ) -> None:
        """Validate a cure while the Clerk holds its durable journal lock."""

        _validate_claim_reduction(
            entries,
            artifacts_root=self._artifacts_root,
            account_id=account_id,
            request=request,
        )

    def apply_adjustment(
        self,
        *,
        account_id: str,
        adjustment: AccountClerkOperatorAdjustment,
        request: JournalCureRequest,
        journal: AccountClerkJournal,
    ) -> JournalCureReceipt:
        """Validate and append through the caller's sole serialized Clerk journal."""

        try:
            entry = journal.append_operator_adjustment(
                adjustment,
                validate_adjustment=lambda entries: self.validate_adjustment(
                    entries, account_id=account_id, request=request
                ),
            )
        except AccountClerkOperatorAdjustmentConflict as exc:
            raise JournalCureError(
                "JOURNAL_CURE_IDEMPOTENCY_CONFLICT",
                "idempotency key was already used with a different cure payload",
            ) from exc
        return _receipt(entry)

    def handler_for_clerk(
        self,
        *,
        account_id: str,
        append_operator_adjustment: JournalCureAppender,
    ) -> JournalCureHandler:
        """Bind cure validation to the Clerk's sole serialized append seam.

        The returned handler is deliberately narrow: the RPC server needs no
        artifact path or concrete cure service to route an operator request.
        """

        async def apply(request: JournalCureRequest) -> JournalCureReceipt:
            adjustment = self.adjustment_for(account_id=account_id, request=request)
            try:
                entry = await append_operator_adjustment(
                    adjustment,
                    validate_adjustment=lambda entries: self.validate_adjustment(
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
            return self.receipt_for(entry)

        return apply

    @staticmethod
    def receipt_for(entry: AccountClerkJournalEntry) -> JournalCureReceipt:
        """Convert the Clerk-owned durable entry to the public receipt."""

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
        binding = index_account_instance_bindings(
            read_account_instance_registry(self._artifacts_root, account_id), account_id=account_id
        ).latest_by_namespace.get(bot_order_namespace)
        if binding is None or binding.lifecycle_state != "RETIRED":
            return JournalCurePreview(
                account_id=account_id,
                bot_order_namespace=bot_order_namespace,
                symbol=normalized_symbol,
                journal_quantity=quantity,
                can_cure=False,
                reason_code="JOURNAL_CURE_NAMESPACE_NOT_PROVEN_RETIRED",
            )
        return JournalCurePreview(
            account_id=account_id,
            bot_order_namespace=bot_order_namespace,
            symbol=normalized_symbol,
            journal_quantity=quantity,
            required_adjustment_sign="negative" if quantity > 0 else "positive",
            can_cure=True,
            reason_code="JOURNAL_CURE_CLAIM_REDUCIBLE",
            confirmation=OperatorConfirmationCopy(
                title="Append Clerk journal cure",
                body="Append the exact operator adjustment shown from the current Clerk claim preview.",
                consequence="The Clerk will durably record this immutable adjustment without changing broker truth.",
                confirm_label="Append journal cure",
            ),
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
    artifacts_root: Path,
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
    binding = index_account_instance_bindings(
        read_account_instance_registry(artifacts_root, account_id), account_id=account_id
    ).latest_by_namespace.get(request.bot_order_namespace)
    if binding is None or binding.lifecycle_state != "RETIRED":
        raise JournalCureError(
            "JOURNAL_CURE_NAMESPACE_NOT_PROVEN_RETIRED",
            "A cure requires a registry-proven retired namespace.",
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
