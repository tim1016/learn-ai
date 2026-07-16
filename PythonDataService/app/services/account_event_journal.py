"""Strict, read-only projections over immutable account event journals."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.engine.live.account_artifacts import AccountArtifactError, AccountEventRecord, read_account_events
from app.engine.live.account_identity import normalize_account_id
from app.schemas.account_events import (
    AccountEventEvidenceRef,
    AccountEventKind,
    AccountEventRow,
    AccountEventsResponse,
    AccountEventView,
)
from app.utils.timestamps import now_ms_utc

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
    "cohort_batch_launch_authorized": (
        "configuration",
        "Account launch authorization was recorded.",
        "Cohort batch launch authorization recorded.",
    ),
    "cohort_batch_launch_outcomes_recorded": (
        "activity",
        "Account launch outcomes were recorded.",
        "Cohort batch launch outcomes recorded.",
    ),
    "cohort_batch_launch_member_start_recorded": (
        "activity",
        "A scheduled cohort start outcome was recorded.",
        "Cohort scheduled member start recorded.",
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
    """Classify the immutable ledger without mutating or repairing it."""

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
        """Return a newest-first cursor page from one account's journal."""

        if before_seq is not None and after_seq is not None:
            raise AccountEventJournalError("before_seq and after_seq cannot be combined")
        canonical_account_id = normalize_account_id(account_id)
        try:
            raw_events = read_account_events(self._artifacts_root, canonical_account_id)
        except AccountArtifactError as exc:
            raise AccountEventJournalError(str(exc)) from exc
        rows = self._project_rows(canonical_account_id, raw_events)
        latest_seq = len(raw_events) or None
        filtered = self._filter_rows(
            rows,
            view=view,
            kinds=kinds,
            before_seq=before_seq,
            after_seq=after_seq,
        )
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

    def _project_rows(self, account_id: str, raw_events: list[dict]) -> list[AccountEventRow]:
        rows: list[AccountEventRow] = []
        expected_seq = 1
        for index, raw_event in enumerate(raw_events, start=1):
            _require_strict_event_envelope(raw_event, index)
            try:
                record = AccountEventRecord.model_validate(raw_event)
            except ValidationError as exc:
                raise AccountEventJournalError(f"invalid account event row {index}: {exc}") from exc
            if record.account_id != account_id:
                raise AccountEventJournalError(f"account event row {index} belongs to another account")
            if record.seq != expected_seq:
                raise AccountEventJournalError(
                    f"account event row {index} has non-contiguous sequence {record.seq}"
                )
            expected_seq += 1
            presentation = _event_presentation(record.event_type, raw_event)
            if presentation is None:
                continue
            kind, trader_narration, operator_detail = presentation
            rows.append(
                AccountEventRow(
                    event_id=f"{account_id}:{record.seq}",
                    seq=record.seq,
                    kind=kind,
                    occurred_at_ms=record.ts_ms,
                    trader_narration=trader_narration,
                    operator_detail=operator_detail,
                    evidence_refs=_evidence_refs(account_id, record.seq, raw_event),
                )
            )
        return rows

    def _filter_rows(
        self,
        rows: list[AccountEventRow],
        *,
        view: AccountEventView,
        kinds: frozenset[AccountEventKind],
        before_seq: int | None,
        after_seq: int | None,
    ) -> list[AccountEventRow]:
        today_ny = _ny_day(self._now_ms()) if view == "trader_today" else None
        filtered: list[AccountEventRow] = []
        for row in rows:
            if before_seq is not None and row.seq >= before_seq:
                continue
            if after_seq is not None and row.seq <= after_seq:
                continue
            if kinds and row.kind not in kinds:
                continue
            if view == "trader_today" and (
                row.trader_narration is None or _ny_day(row.occurred_at_ms) != today_ny
            ):
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
    event_type: str,
    raw_event: Mapping[str, object],
) -> tuple[AccountEventKind, str | None, str] | None:
    """Hide steady-state diagnostics while preserving every durable raw row."""

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
    return _EVENT_PRESENTATION.get(
        event_type,
        ("other", None, "An unclassified account journal event was recorded."),
    )


def _evidence_refs(
    account_id: str,
    seq: int,
    raw_event: Mapping[str, object],
) -> list[AccountEventEvidenceRef]:
    refs = [AccountEventEvidenceRef(source="account_event_journal", ref=f"{account_id}:{seq}")]
    for field, source in _EVIDENCE_FIELDS:
        value = raw_event.get(field)
        for ref in _string_values(value):
            refs.append(AccountEventEvidenceRef(source=source, ref=ref))
    return refs


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
