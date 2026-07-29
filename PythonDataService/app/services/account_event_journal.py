"""Strict, read-only projections over merged Account Desk operational history."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_artifacts import AccountArtifactError, read_legacy_account_events
from app.engine.live.account_clerk_journal import read_account_clerk_journal
from app.engine.live.account_clerk_journal_models import AccountClerkJournalCorruptError
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.producer_operational_log import (
    MergedOperationalHistoryEvent,
    ProducerOperationalLogError,
    merge_operator_history,
    order_operator_history,
    read_producer_operational_events,
)
from app.schemas.account_events import (
    AccountEventEvidenceRef,
    AccountEventKind,
    AccountEventOperatorOrderReceipt,
    AccountEventRow,
    AccountEventsResponse,
    AccountEventView,
    TraderAccountEventRow,
    TraderAccountEventsResponse,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")

_EVENT_PRESENTATION: dict[str, tuple[AccountEventKind, str | None, str]] = {
    "account_freeze_recorded": (
        "safety",
        "An account safety freeze was recorded.",
        "Account safety freeze recorded in the journal.",
    ),
    "account_freeze_cleared": (
        "safety",
        "An account safety freeze was cleared.",
        "Account safety freeze clearance recorded in the journal.",
    ),
    "account_recovery_proof_recorded": (
        "safety",
        "Recovery proof was recorded for this account.",
        "Account recovery proof recorded in the journal.",
    ),
    "account_audited_override_recorded": (
        "safety",
        "An account override was recorded.",
        "Audited account override recorded in the journal.",
    ),
    "account_reconciliation_receipt_recorded": (
        "reconciliation",
        "Account reconciliation was recorded.",
        "Account reconciliation receipt recorded in the journal.",
    ),
    "account_reconciliation_invalidated": (
        "reconciliation",
        "New account activity requires reconciliation.",
        "Account reconciliation was invalidated by new broker activity.",
    ),
    "account_reconciliation_automation_policy_updated": (
        "configuration",
        None,
        "Account reconciliation automation policy changed.",
    ),
    "account_clerk_reconciliation_resolved": (
        "reconciliation",
        "Account reconciliation completed.",
        "Account service reconciliation completed.",
    ),
    "account_clerk_reconciliation_unhealthy": (
        "reconciliation",
        "Account reconciliation needs attention.",
        "Account service reconciliation reported an unhealthy result.",
    ),
    "account_clerk_reconciliation_iteration_failed": (
        "reconciliation",
        "Account reconciliation needs attention.",
        "Account service reconciliation iteration failed.",
    ),
    "account_clerk_operator_recovery_flatten": (
        "safety",
        "Account recovery flatten completed.",
        "Account service recovery flatten event recorded.",
    ),
    "account_clerk_event_stream_down": (
        "safety",
        "Broker event evidence is unavailable.",
        "Account service broker event stream is unavailable.",
    ),
    "account_clerk_unattributed_broker_event": (
        "activity",
        "Unattributed broker activity needs review.",
        "Account service recorded unattributed broker activity.",
    ),
    "account_clerk_manual_order_acked": (
        "activity",
        "Your paper order was received by the broker.",
        "Account Clerk recorded a durable IBKR acknowledgement for a manual paper order.",
    ),
    "account_owner_generation_recorded": (
        "clerk",
        "Account connection ownership was updated.",
        "Account Owner generation recorded.",
    ),
    "account_clerk_generation_recorded": (
        "clerk",
        None,
        "Account service generation recorded.",
    ),
    "account_clerk_restore_completed": (
        "clerk",
        "Account Clerk restore completed.",
        "The host daemon restored the sole Account Clerk and the cockpit re-observed its generation.",
    ),
    "account_observation_lease_verified": (
        "reconciliation",
        "Account verification is current.",
        "Account observation returned to a verified state.",
    ),
    "account_observation_lease_revoked": (
        "safety",
        "Account verification needs attention.",
        "Account observation proof was revoked.",
    ),
    "account_clerk_event_stream_recovered": (
        "reconciliation",
        "Broker event evidence recovered.",
        "The Account service broker event stream recovered.",
    ),
    "account_clerk_journal_authority_drift_detected": (
        "safety",
        "Account ledger evidence diverged.",
        "The account ledger and legacy comparison diverged and require review.",
    ),
    "account_instance_binding_recorded": (
        "configuration",
        None,
        "Account instance binding recorded.",
    ),
}

_EVIDENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("receipt_id", "receipt"),
    ("recovery_id", "recovery"),
    ("run_id", "run"),
    ("strategy_instance_id", "strategy_instance"),
    ("bot_order_namespace", "bot_order_namespace"),
    ("execution_ids", "execution"),
    ("order_ids", "order"),
    ("order_id", "order"),
    ("perm_id", "order"),
    ("exec_id", "execution"),
    ("execution_id", "execution"),
    ("order_ref", "order_ref"),
    ("client_order_id", "order"),
    ("intent_id", "intent"),
    ("intent_ids", "intent"),
)


class AccountEventJournalError(ValueError):
    """A strict Account desk event projection cannot be safely produced."""


class AccountEventJournalService:
    """Classify display history without mutating, repairing, or creating authority."""

    def __init__(self, *, artifacts_root: Path, now_ms: Callable[[], int] = now_ms_utc) -> None:
        self._artifacts_root = artifacts_root
        self._now_ms = now_ms

    def page(
        self,
        *,
        account_id: str,
        view: AccountEventView,
        limit: int,
        kinds: frozenset[AccountEventKind],
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> AccountEventsResponse:
        """Return a newest-first cursor page from one account's display history."""

        if before_seq is not None and after_seq is not None:
            raise AccountEventJournalError("before_seq and after_seq cannot be combined")
        canonical_account_id = normalize_account_id(account_id)
        try:
            history = self._read_history(canonical_account_id)
        except (AccountArtifactError, AccountClerkJournalCorruptError, ProducerOperationalLogError) as exc:
            raise AccountEventJournalError(str(exc)) from exc
        rows = self._project_rows(canonical_account_id, history)
        latest_seq = len(history) or None
        filtered = self._filter_rows(rows, kinds=kinds, before_seq=before_seq, after_seq=after_seq)
        newest_first = list(reversed(filtered))
        page_rows = newest_first[:limit]
        next_before_seq = self._next_before_seq(newest_first, page_rows)
        return AccountEventsResponse(
            account_id=canonical_account_id,
            view=view,
            rows=page_rows,
            latest_seq=latest_seq,
            next_before_seq=next_before_seq,
        )

    def trader_page(self, *, account_id: str, limit: int) -> TraderAccountEventsResponse:
        """Return backend-authored trader outcomes without projecting any receipt fields."""

        canonical_account_id = normalize_account_id(account_id)
        try:
            history = self._read_history(canonical_account_id)
        except (AccountArtifactError, AccountClerkJournalCorruptError, ProducerOperationalLogError) as exc:
            raise AccountEventJournalError(str(exc)) from exc
        rows = self._project_rows(canonical_account_id, history)
        today_ny = _ny_day(self._now_ms())
        trader_rows = [
            TraderAccountEventRow(
                event_id=row.event_id,
                seq=row.seq,
                occurred_at_ms=row.occurred_at_ms,
                outcome=row.trader_narration,
            )
            for row in reversed(rows)
            if row.trader_narration is not None and _ny_day(row.occurred_at_ms) == today_ny
        ][:limit]
        return TraderAccountEventsResponse(
            account_id=canonical_account_id,
            rows=trader_rows,
            latest_seq=len(history) or None,
            next_before_seq=None,
        )

    def _read_history(self, account_id: str) -> tuple[MergedOperationalHistoryEvent, ...]:
        operational_history = merge_operator_history(
            legacy_events=read_legacy_account_events(self._artifacts_root, account_id),
            producer_records=read_producer_operational_events(
                self._artifacts_root,
                account_id=account_id,
            ),
        )
        return order_operator_history(
            (
                *operational_history,
                *_manual_order_ack_history_from_clerk_journal(
                    self._artifacts_root,
                    account_id,
                ),
            )
        )

    def _project_rows(
        self,
        account_id: str,
        history: tuple[MergedOperationalHistoryEvent, ...],
    ) -> list[AccountEventRow]:
        rows: list[AccountEventRow] = []
        for display_seq, event in enumerate(history, start=1):
            if event.account_id != account_id:
                raise AccountEventJournalError("operator history row belongs to another account")
            raw_event = {**event.payload, "account_id": event.account_id, "event_type": event.event_type}
            presentation = _event_presentation(event, raw_event)
            if presentation is None:
                continue
            kind, trader_narration, operator_detail = presentation
            rows.append(
                AccountEventRow(
                    event_id=event.event_id,
                    provenance=event.source,
                    seq=display_seq,
                    kind=kind,
                    occurred_at_ms=event.display_clock_ms,
                    event_at_ms=event.event_at_ms,
                    arrived_at_ms=event.arrived_at_ms,
                    recorded_at_ms=event.recorded_at_ms,
                    producer=event.producer,
                    producer_boot_id=event.producer_boot_id,
                    producer_seq=event.producer_seq,
                    trader_narration=trader_narration,
                    operator_detail=operator_detail,
                    evidence_refs=_evidence_refs(event, raw_event),
                    operator_order_receipt=_operator_order_receipt(
                        event,
                        raw_event,
                        display_seq,
                    ),
                )
            )
        return rows

    def _filter_rows(
        self,
        rows: list[AccountEventRow],
        *,
        kinds: frozenset[AccountEventKind],
        before_seq: int | None,
        after_seq: int | None,
    ) -> list[AccountEventRow]:
        filtered: list[AccountEventRow] = []
        for row in rows:
            if before_seq is not None and row.seq >= before_seq:
                continue
            if after_seq is not None and row.seq <= after_seq:
                continue
            if kinds and row.kind not in kinds:
                continue
            filtered.append(row)
        return filtered

    @staticmethod
    def _next_before_seq(
        filtered_newest_first: list[AccountEventRow],
        page_rows: list[AccountEventRow],
    ) -> int | None:
        if not page_rows or len(page_rows) == len(filtered_newest_first):
            return None
        return page_rows[-1].seq


def _ny_day(timestamp_ms: int) -> datetime.date:
    try:
        return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).astimezone(_NY_TZ).date()
    except (OSError, OverflowError, ValueError) as exc:
        raise AccountEventJournalError("account event timestamp is outside the supported calendar") from exc


def _event_presentation(
    event: MergedOperationalHistoryEvent,
    raw_event: Mapping[str, object],
) -> tuple[AccountEventKind, str | None, str] | None:
    """Hide steady-state diagnostics while preserving every durable raw row."""

    event_type = event.event_type
    if event_type == "account_clerk_sidecar_journal_parity":
        # This is the bounded shadow comparator used to qualify authority, not
        # an operator transition. Post-cutover drift has its own durable
        # ``account_clerk_journal_authority_drift_detected`` event and remedy.
        return None
    if event_type == "account_observation_lease_shadow_comparison":
        if raw_event.get("truth_status") == raw_event.get("lease_status"):
            return None
        return (
            "safety",
            "Account verification comparison diverged.",
            "The current Account Truth gate and observation proof do not agree.",
        )
    if event_type == "account_clerk_manual_order_acked" and event.source != "clerk_journal":
        return (
            "other",
            None,
            "A non-authoritative operational row referenced a manual order; inspect the Clerk journal.",
        )
    if event_type == "account_clerk_manual_order_unprojectable":
        return (
            "other",
            None,
            "A manual order receipt could not be projected from the Clerk journal and is shown "
            "unclassified; inspect the Clerk journal.",
        )
    return _EVENT_PRESENTATION.get(
        event_type,
        ("other", None, "An unclassified account journal event was recorded."),
    )


def _evidence_refs(
    event: MergedOperationalHistoryEvent,
    raw_event: Mapping[str, object],
) -> list[AccountEventEvidenceRef]:
    source = "account_event_journal" if event.source == "legacy_account_events" else event.source
    refs = [AccountEventEvidenceRef(source=source, ref=event.event_id)]
    for field, source in _EVIDENCE_FIELDS:
        value = raw_event.get(field)
        for ref in _string_values(value):
            refs.append(AccountEventEvidenceRef(source=source, ref=ref))
    return refs


def _operator_order_receipt(
    event: MergedOperationalHistoryEvent,
    raw_event: Mapping[str, object],
    index: int,
) -> AccountEventOperatorOrderReceipt | None:
    """Read a manual receipt only when it came from the canonical Clerk journal."""

    if event.source != "clerk_journal" or event.event_type != "account_clerk_manual_order_acked":
        return None
    try:
        return AccountEventOperatorOrderReceipt.model_validate(
            {
                "broker": raw_event.get("broker"),
                "order_id": raw_event.get("order_id"),
                "perm_id": raw_event.get("perm_id"),
                "order_ref": raw_event.get("order_ref"),
                "symbol": raw_event.get("symbol"),
                "action": raw_event.get("action"),
                "quantity": raw_event.get("quantity"),
                "order_type": raw_event.get("order_type"),
                "limit_price": raw_event.get("limit_price"),
                "status": raw_event.get("status"),
                "acknowledged_at_ms": raw_event.get("acknowledged_at_ms"),
            }
        )
    except ValidationError as exc:
        raise AccountEventJournalError(
            f"manual order receipt in account event row {index} is invalid: {exc}"
        ) from exc


def _manual_order_ack_history_from_clerk_journal(
    artifacts_root: Path,
    account_id: str,
) -> tuple[MergedOperationalHistoryEvent, ...]:
    """Project manual acknowledgements directly from canonical Clerk receipts.

    This is intentionally a read-only projection.  The old Account Desk mirror
    is retained only as historical evidence, and a producer log is never
    permitted to originate an operator-visible order receipt.
    """

    history: list[MergedOperationalHistoryEvent] = []
    for entry in read_account_clerk_journal(artifacts_root, account_id):
        intent = entry.intent
        if (
            entry.entry_kind != "broker_acked"
            or intent is None
            or intent.intent_kind != "MANUAL_ORDER"
            or entry.order_id is None
        ):
            continue
        ack = entry.broker_ack
        acknowledged_at_ms = ack.placed_at_ms if ack is not None else entry.recorded_at_ms
        try:
            spec = IbkrOrderSpec.model_validate(intent.order_spec)
        except ValidationError as exc:
            # A single unprojectable receipt must not blank the whole operator
            # Desk. Surface it as an unclassified row and keep the rest visible.
            logger.warning(
                "clerk manual order receipt %s has an unprojectable order_spec; "
                "surfacing an unclassified row instead of failing the Desk: %s",
                entry.seq,
                exc,
            )
            history.append(
                MergedOperationalHistoryEvent(
                    event_id=f"clerk:{account_id}:{entry.seq}",
                    source="clerk_journal",
                    account_id=account_id,
                    event_type="account_clerk_manual_order_unprojectable",
                    display_clock_ms=acknowledged_at_ms,
                    event_at_ms=ack.placed_at_ms if ack is not None else None,
                    recorded_at_ms=entry.recorded_at_ms,
                    producer="clerk_journal",
                    producer_boot_id="canonical",
                    producer_seq=entry.seq,
                    payload={
                        "intent_id": intent.intent_id,
                        "order_ref": intent.order_ref,
                        "order_id": entry.order_id,
                    },
                )
            )
            continue
        history.append(
            MergedOperationalHistoryEvent(
                event_id=f"clerk:{account_id}:{entry.seq}",
                source="clerk_journal",
                account_id=account_id,
                event_type="account_clerk_manual_order_acked",
                display_clock_ms=acknowledged_at_ms,
                event_at_ms=ack.placed_at_ms if ack is not None else None,
                recorded_at_ms=entry.recorded_at_ms,
                producer="clerk_journal",
                producer_boot_id="canonical",
                producer_seq=entry.seq,
                payload={
                    "broker": "ibkr",
                    "intent_id": intent.intent_id,
                    "order_ref": intent.order_ref,
                    "order_id": entry.order_id,
                    "perm_id": entry.perm_id,
                    "exec_id": entry.exec_id,
                    "symbol": ack.symbol if ack is not None else spec.symbol,
                    "action": ack.action if ack is not None else spec.action,
                    "quantity": ack.quantity if ack is not None else spec.quantity,
                    "order_type": ack.order_type if ack is not None else spec.order_type,
                    "limit_price": ack.limit_price if ack is not None else spec.limit_price,
                    "status": ack.status if ack is not None else "Acknowledged",
                    "acknowledged_at_ms": acknowledged_at_ms,
                },
            )
        )
    return tuple(history)


def _string_values(value: object) -> Iterable[str]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, int) and not isinstance(value, bool):
        return (str(value),)
    if isinstance(value, list):
        return tuple(
            item if isinstance(item, str) else str(item)
            for item in value
            if (isinstance(item, str) and item) or (isinstance(item, int) and not isinstance(item, bool))
        )
    return ()


def _require_strict_event_envelope(raw_event: Mapping[str, object], index: int) -> None:
    """Reject permissive Pydantic coercion at the durable artifact boundary."""

    for field in ("account_id", "event_type"):
        value = raw_event.get(field)
        if not isinstance(value, str) or not value:
            raise AccountEventJournalError(f"account event row {index} has invalid {field}")
    for field in ("seq", "ts_ms"):
        value = raw_event.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AccountEventJournalError(f"account event row {index} has invalid {field}")
