"""Repair broker-activity rows for historical live runs.

PR4 of issue #684: damaged historical runs may have durable broker facts
(``broker_callbacks.jsonl`` on new runs, ``executions.parquet`` on legacy
runs) but no per-instance ``broker_activity.jsonl`` rows for the cockpit.
This module reconstructs missing operator-facing rows without overwriting
live-captured evidence.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.broker_callbacks import (
    BrokerCallbackWal,
    BrokerCallbackWalCorruptError,
    broker_callbacks_wal_path,
)
from app.engine.live.intent_ledger import LedgerProjection
from app.engine.live.intent_ledger import fold as fold_intent_events
from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.engine.live.run_ledger import read_ledger
from app.schemas.broker_activity import (
    BrokerActivityRow,
    ReconciliationTimingPolicy,
    SizingProvenance,
)
from app.services.broker_activity_reconciler import (
    EngineIntent,
    ReconciliationContext,
    UnauthorableEventError,
    author_row_from_event,
    match_identity,
    parse_order_ref,
)
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    instance_broker_activity_wal_path,
)

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,127}$")

ReconstructionSource = Literal["raw_callback_wal", "legacy_execution_artifacts"]


@dataclass(frozen=True)
class BrokerActivityReconstructionResult:
    run_id: str
    strategy_instance_id: str
    source: ReconstructionSource
    rows_written: int
    rows_skipped_existing: int
    target_wal_path: Path


@dataclass(frozen=True)
class _SourceEvent:
    event: IbkrOrderEvent
    source_seq: int | None


def reconstruct_broker_activity_for_run(
    run_id: str,
    *,
    artifacts_root: Path,
    timing_policy: ReconciliationTimingPolicy | None = None,
) -> BrokerActivityReconstructionResult:
    """Append missing reconstructed broker-activity rows for ``run_id``.

    Preferred source is ``<run_dir>/broker_callbacks.jsonl``. If that WAL
    is absent or empty, legacy runs are reconstructed from
    ``executions.parquet``. Existing rows in the per-instance WAL are
    treated as authoritative and are never overwritten.
    """
    run_dir = _resolve_run_dir(run_id, artifacts_root)
    ledger = read_ledger(run_dir / "run_ledger.json")
    if not ledger.strategy_instance_id:
        raise ValueError(f"run {run_id!r} has no strategy_instance_id in run_ledger.json")

    strategy_instance_id = ledger.strategy_instance_id
    envelope = _read_envelope(artifacts_root, strategy_instance_id)
    bot_order_namespace = _bot_order_namespace(strategy_instance_id, envelope)
    submitted_orders = _submitted_orders(run_dir, envelope)
    intent_by_id = _intent_by_id(run_dir, envelope)

    source, source_events = _source_events(run_dir)
    target_wal_path = instance_broker_activity_wal_path(artifacts_root, strategy_instance_id)
    wal = BrokerActivityWal(target_wal_path)
    existing_keys = _existing_row_keys(wal.read_all())

    rows_written = 0
    rows_skipped_existing = 0
    for source_event in source_events:
        event = _enrich_event_identity(source_event.event, submitted_orders)
        if source == "legacy_execution_artifacts":
            event = _enrich_legacy_fill_shape(event, intent_by_id)
        key = _event_key(event)
        if key in existing_keys:
            rows_skipped_existing += 1
            continue
        row = _author_reconstructed_row(
            wal=wal,
            event=event,
            source_run_id=run_id,
            source_seq=source_event.source_seq,
            source=source,
            bot_order_namespace=bot_order_namespace,
            submitted_orders=submitted_orders,
            intent_by_id=intent_by_id,
            timing_policy=timing_policy or ReconciliationTimingPolicy(),
        )
        if row is None:
            continue
        existing_keys.add(key)
        rows_written += 1

    return BrokerActivityReconstructionResult(
        run_id=run_id,
        strategy_instance_id=strategy_instance_id,
        source=source,
        rows_written=rows_written,
        rows_skipped_existing=rows_skipped_existing,
        target_wal_path=target_wal_path,
    )


def _enrich_event_identity(
    event: IbkrOrderEvent, submitted_orders: dict[str, dict[str, Any]]
) -> IbkrOrderEvent:
    if event.order_ref and event.order_type:
        return event
    for entry in submitted_orders.values():
        if event.perm_id is not None and _as_int_or_none(entry.get("perm_id")) == event.perm_id:
            return _copy_identity_from_entry(event, entry)
        if event.order_id and _as_int_or_none(entry.get("order_id")) == event.order_id:
            return _copy_identity_from_entry(event, entry)
    return event


def _copy_identity_from_entry(event: IbkrOrderEvent, entry: dict[str, Any]) -> IbkrOrderEvent:
    updates: dict[str, Any] = {}
    order_ref = _as_str_or_none(entry.get("order_ref"))
    if order_ref and not event.order_ref:
        updates["order_ref"] = order_ref
    order_type = _as_str_or_none(entry.get("order_type"))
    if order_type and not event.order_type:
        updates["order_type"] = order_type
    return event.model_copy(update=updates) if updates else event


def _enrich_legacy_fill_shape(
    event: IbkrOrderEvent, intent_by_id: dict[str, EngineIntent]
) -> IbkrOrderEvent:
    parsed_ref = parse_order_ref(event.order_ref)
    if parsed_ref is None:
        return event
    intent = intent_by_id.get(parsed_ref[1])
    if intent is None or intent.requested_qty is None or event.fill_quantity is None:
        return event
    remaining = max(abs(intent.requested_qty) - abs(event.fill_quantity), 0.0)
    return event.model_copy(update={"remaining": remaining})


def _resolve_run_dir(run_id: str, artifacts_root: Path) -> Path:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(f"invalid run_id: {run_id!r}")
    live_runs_root = (artifacts_root / "live_runs").resolve()
    candidate = (live_runs_root / run_id).resolve(strict=False)
    try:
        common = os.path.commonpath([str(candidate), str(live_runs_root)])
    except ValueError as exc:
        raise ValueError(f"run {run_id!r} is outside live_runs root") from exc
    if common != str(live_runs_root):
        raise ValueError(f"run {run_id!r} escapes live_runs root")
    if not candidate.is_dir():
        raise FileNotFoundError(f"live run directory not found: {candidate}")
    return candidate


def _read_envelope(
    artifacts_root: Path, strategy_instance_id: str
) -> LiveStateEnvelope | None:
    try:
        return LiveStateSidecarRepo(
            stable_live_state_path(artifacts_root, strategy_instance_id)
        ).read()
    except LiveStateSidecarCorruptError:
        return None


def _bot_order_namespace(
    strategy_instance_id: str, envelope: LiveStateEnvelope | None
) -> str:
    if envelope is not None and envelope.bot_order_namespace:
        return envelope.bot_order_namespace
    return f"learn-ai/{strategy_instance_id}/v1"


def _submitted_orders(
    run_dir: Path, envelope: LiveStateEnvelope | None
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    if envelope is not None:
        merged.update(envelope.submitted_orders)
    merged.update(_fold_intent_wal(run_dir))
    return merged


def _fold_intent_wal(run_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        events = IntentWal(run_dir / "intent_events.jsonl").read_tail()
    except IntentWalCorruptError as exc:
        raise ValueError(f"cannot reconstruct from corrupt intent WAL: {exc}") from exc
    except OSError:
        return {}
    if not events:
        return {}
    view = fold_intent_events(LedgerProjection(), events)
    out: dict[str, dict[str, Any]] = {}
    for intent_id, order_view in view.submitted_orders.items():
        entry: dict[str, Any] = {
            "status": order_view.status.value,
            "order_ref": order_view.order_ref,
            "bot_order_namespace": order_view.bot_order_namespace,
        }
        if order_view.order_id is not None:
            entry["order_id"] = order_view.order_id
        if order_view.perm_id is not None:
            entry["perm_id"] = order_view.perm_id
        if order_view.order_spec is not None:
            entry.update({k: v for k, v in order_view.order_spec.items() if k not in entry})
        out[intent_id] = entry
    return out


def _intent_by_id(
    run_dir: Path, envelope: LiveStateEnvelope | None
) -> dict[str, EngineIntent]:
    submitted = _submitted_orders(run_dir, envelope)
    sizing_by_intent: dict[str, SizingProvenance] = {}
    if envelope is not None:
        for entry in envelope.sizing_resolutions:
            intent_id = entry.get("intent_id")
            if isinstance(intent_id, str):
                sizing_by_intent[intent_id] = SizingProvenance.model_validate(
                    {
                        k: entry.get(k)
                        for k in (
                            "policy",
                            "requested_qty",
                            "reference_price_decimal_str",
                            "provenance",
                            "surface",
                            "skip_reason",
                        )
                        if k in entry
                    }
                )
    out: dict[str, EngineIntent] = {}
    for intent_id, entry in submitted.items():
        out[intent_id] = EngineIntent(
            intent_id=intent_id,
            mutation_attempt_id=_as_str_or_none(entry.get("mutation_attempt_id")),
            requested_qty=_as_float_or_none(
                entry.get("requested_qty", entry.get("quantity"))
            ),
            requested_price=_as_float_or_none(
                entry.get("requested_price", entry.get("limit_price"))
            ),
            intent_created_ms=_as_int_or_none(entry.get("intent_created_ms")),
            dispatched_ms=_as_int_or_none(entry.get("dispatched_ms")),
            acked_ms=_as_int_or_none(entry.get("acked_ms")),
            sizing_provenance=sizing_by_intent.get(intent_id),
        )
    return out


def _source_events(run_dir: Path) -> tuple[ReconstructionSource, list[_SourceEvent]]:
    raw_path = broker_callbacks_wal_path(run_dir)
    if raw_path.exists():
        try:
            records = BrokerCallbackWal(raw_path).read_all()
        except BrokerCallbackWalCorruptError as exc:
            raise ValueError(f"cannot reconstruct from corrupt raw callback WAL: {exc}") from exc
        events = [
            _SourceEvent(record.event, record.seq)
            for record in records
            if _event_authors_activity(record.event)
        ]
        if events:
            return "raw_callback_wal", events

    legacy_events = _legacy_execution_events(run_dir)
    if legacy_events:
        return "legacy_execution_artifacts", legacy_events
    return "legacy_execution_artifacts", []


def _event_authors_activity(event: IbkrOrderEvent) -> bool:
    if event.event_type in ("fill", "cancel", "error"):
        return True
    return event.event_type == "status" and (event.status or "") in (
        "Cancelled",
        "ApiCancelled",
        "Rejected",
    )


def _legacy_execution_events(run_dir: Path) -> list[_SourceEvent]:
    path = run_dir / "executions.parquet"
    if not path.exists():
        return []
    frame = pd.read_parquet(path)
    events: list[_SourceEvent] = []
    for idx, row in enumerate(frame.to_dict(orient="records"), start=1):
        if row.get("execution_source") not in (None, "broker_fill"):
            continue
        quantity = _as_float_or_none(row.get("fill_quantity"))
        price = _as_float_or_none(row.get("fill_price"))
        ts_ms = _as_int_or_none(row.get("ts_ms"))
        if quantity is None or price is None or ts_ms is None:
            continue
        side = "BUY" if quantity >= 0 else "SELL"
        order_ref = _as_str_or_none(row.get("client_order_id"))
        event = IbkrOrderEvent(
            account_id=_as_str_or_none(row.get("account_id")) or "",
            order_id=_order_id_from_legacy_row(row),
            perm_id=_as_int_or_none(row.get("perm_id")),
            event_type="fill",
            status="Filled",
            order_ref=order_ref if order_ref and ":" in order_ref else None,
            symbol=_as_str_or_none(row.get("symbol")) or "",
            side=side,
            order_type=_as_str_or_none(row.get("order_type")),
            exec_id=_as_str_or_none(row.get("exec_id")),
            fill_quantity=abs(quantity),
            avg_fill_price=price,
            cumulative_filled=abs(quantity),
            remaining=_as_float_or_none(row.get("remaining")),
            last_fill_price=price,
            exec_time_ms=_as_int_or_none(row.get("exec_time_ms")) or ts_ms,
            fee=_as_float_or_none(row.get("fee")),
            ts_ms=ts_ms,
        )
        events.append(_SourceEvent(event, idx))
    return events


def _order_id_from_legacy_row(row: dict[str, Any]) -> int:
    client_order_id = _as_str_or_none(row.get("client_order_id"))
    if client_order_id:
        _, _, suffix = client_order_id.rpartition("-")
        parsed = _as_int_or_none(suffix)
        if parsed is not None:
            return parsed
    value = _as_int_or_none(row.get("perm_id"))
    if value is not None:
        return value
    return 0


def _author_reconstructed_row(
    *,
    wal: BrokerActivityWal,
    event: IbkrOrderEvent,
    source_run_id: str,
    source_seq: int | None,
    source: ReconstructionSource,
    bot_order_namespace: str,
    submitted_orders: dict[str, dict[str, Any]],
    intent_by_id: dict[str, EngineIntent],
    timing_policy: ReconciliationTimingPolicy,
) -> BrokerActivityRow | None:
    parsed_ref = parse_order_ref(event.order_ref)
    if parsed_ref is not None and parsed_ref[0] != bot_order_namespace:
        return None
    intent_id = match_identity(
        event,
        submitted_orders=submitted_orders,
        bot_order_namespace=bot_order_namespace,
    )
    if intent_id is None and event.event_type not in ("fill", "cancel", "error"):
        return None
    if intent_id is not None and event.event_type == "status" and (event.status or "") not in (
        "Cancelled",
        "ApiCancelled",
        "Rejected",
    ):
        return None

    seq = wal.allocate_seq()
    ctx = ReconciliationContext(
        seq=seq,
        ts_ms=event.ts_ms,
        bot_order_namespace=bot_order_namespace,
        timing_policy=timing_policy,
    )
    try:
        row = author_row_from_event(
            event=event,
            intent=intent_by_id.get(intent_id) if intent_id else None,
            ctx=ctx,
        )
    except UnauthorableEventError:
        return None

    repaired = row.model_copy(
        update={
            "source_run_id": source_run_id,
            "source_seq": source_seq,
            "recovery_provenance": "reconstructed",
            "recovery_reason": (
                "raw_callback_wal_reprojection"
                if source == "raw_callback_wal"
                else "legacy_artifacts_missing_activity_wal"
            ),
        }
    )
    wal.append_row(repaired)
    return repaired


def _existing_row_keys(rows: list[BrokerActivityRow]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in rows:
        if row.exec_id:
            keys.add(("exec_id", row.exec_id))
        elif row.order_ref:
            keys.add(("lifecycle", f"{row.order_ref}|{row.template_key}"))
    return keys


def _event_key(event: IbkrOrderEvent) -> tuple[str, str]:
    if event.exec_id:
        return ("exec_id", event.exec_id)
    if event.order_ref:
        return ("lifecycle", f"{event.order_ref}|{_lifecycle_template_key(event)}")
    return (
        "raw",
        "|".join(
            [
                str(event.order_id),
                str(event.perm_id or ""),
                _lifecycle_template_key(event),
            ]
        ),
    )


def _lifecycle_template_key(event: IbkrOrderEvent) -> str:
    status = (event.status or "").lower()
    if event.event_type == "cancel" or status == "cancelled":
        return "cancellation"
    if event.event_type == "error" or status in {"rejected", "apicancelled"}:
        return "rejection"
    return event.event_type


def _as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _as_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out):
        return None
    return int(out)


def _as_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value)
    return text or None


__all__ = [
    "BrokerActivityReconstructionResult",
    "reconstruct_broker_activity_for_run",
]
