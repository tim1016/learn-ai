"""Freeze the cross-engine parity verdict when the LEAN companion lands (PRD #1929).

A port of the retired .NET ``ParityVerdictService``: the verdict compares
the Python engine run (left) with its LEAN companion (right) on four axes —
trade reconciliation (``lean_sidecar_compare_service.reconcile_trade_lists``,
the same classifier the .NET service reached over HTTP), the LEAN-native
metric-reproduction receipt persisted with the companion, the two runs'
production-readiness envelopes, and the compatibility inputs (data policy,
cash, window, fill mode). Nothing numerical is recomputed here; the
receipts are verified, not re-derived.

State machine: ``pending -> agree | diverged | unavailable`` here;
``pending -> run_failed | persist_failed`` via the mark-failed write in the
repository. Every transition is conditional on ``pending`` — the first
terminal state wins and is never overwritten.

Canonical implementation: this file.
Validated against: tests/research/backtest_runs/test_parity.py (ported from
Backend.Tests/Unit/Services/ParityVerdictServiceTests.cs).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import asyncpg

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs.records import trades_as_compare_payload
from app.research.backtest_runs.repository import RunDetail
from app.services.lean_sidecar_compare_service import reconcile_trade_lists
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

VERDICT_VERSION = repo.PARITY_VERDICT_VERSION
FILL_PRICE_ATOL = "0.01"
READINESS_FIELDS: tuple[str, ...] = (
    "verdict_version",
    "status",
    "composite",
    "grade",
    "signal",
    "red_flags",
    "dimensions",
    "missing_metrics",
    "missing_required_metrics",
    "available_required_metrics",
    "required_metrics",
    "normalized_weights",
)
DATA_POLICY_FIELDS: tuple[str, ...] = (
    "source",
    "symbol",
    "adjusted",
    "session",
    "input_bars",
    "strategy_bars",
    "timestamp_policy",
    "timezone",
    "provider_kind",
    "fixture_id",
    "fixture_sha256",
)


@dataclass(frozen=True, slots=True)
class NativeMetricParityReceipt:
    status: str
    reason: str | None
    contract_id: str | None
    source_commit: str | None
    absolute_tolerance: float | None
    native_metric_count: int
    formatted_metric_count: int
    divergence_count: int

    @classmethod
    def unavailable(cls, reason: str) -> NativeMetricParityReceipt:
        return cls("unavailable", reason, None, None, None, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class ReadinessParityReceipt:
    status: str
    reason: str | None
    compared_field_count: int
    mismatched_fields: tuple[str, ...]

    @classmethod
    def unavailable(cls, reason: str) -> ReadinessParityReceipt:
        return cls("unavailable", reason, 0, ())


@dataclass(frozen=True, slots=True)
class InputParityReceipt:
    status: str
    reason: str | None
    compared_field_count: int
    fixture_id: str | None
    fixture_sha256: str | None
    mismatched_fields: tuple[str, ...]

    @classmethod
    def unavailable(cls, reason: str) -> InputParityReceipt:
        return cls("unavailable", reason, 0, None, None, ())


@dataclass(frozen=True, slots=True)
class ParityVerdict:
    status: str
    verdict_json: str


def compute_parity_verdict(*, parity_group_id: str, left: RunDetail, right: RunDetail) -> ParityVerdict:
    """The frozen verdict for a group whose companion has landed. Pure."""
    comparison = reconcile_trade_lists(
        left_trades=trades_as_compare_payload(left.trades),
        right_trades=trades_as_compare_payload(right.trades),
        fill_price_atol=Decimal(FILL_PRICE_ATOL),
    )
    native = read_native_metric_parity(right.lean_statistics_json)
    readiness = compare_readiness(left.run_verdict_json, right.run_verdict_json)
    inputs = compare_inputs(left, right)
    status = _resolve_status(len(comparison.divergences), native.status, readiness.status, inputs.status)
    reason = _resolve_reason(status, native.status, readiness.status, inputs.status)
    counts: dict[str, int] = {}
    for divergence in comparison.divergences:
        counts[divergence.category] = counts.get(divergence.category, 0) + 1
    verdict = {
        "schema_version": 2,
        "parity_group_id": parity_group_id,
        "left_execution_id": left.id,
        "right_execution_id": right.id,
        "engines": {"left": "python", "right": "lean"},
        "status": status,
        "reason": reason,
        "tolerances": {"fill_price_atol": FILL_PRICE_ATOL},
        "native_metric_parity": asdict(native),
        "readiness_parity": asdict(readiness),
        "input_parity": asdict(inputs),
        "divergences": [
            {
                "category": d.category,
                "trade_number": d.trade_number,
                "ms_utc": d.ms_utc,
                "message": d.message,
            }
            for d in comparison.divergences
        ],
        "counts_by_category": counts,
        "computed_at_ms": now_ms_utc(),
    }
    return ParityVerdict(status=status, verdict_json=json.dumps(verdict))


def _resolve_status(trade_divergences: int, native: str, readiness: str, inputs: str) -> str:
    if trade_divergences > 0 or "mismatch" in (native, readiness, inputs):
        return "diverged"
    if native != "match" or readiness != "match" or inputs != "match":
        return "unavailable"
    return "agree"


def _resolve_reason(status: str, native: str, readiness: str, inputs: str) -> str | None:
    if status == "agree":
        return None
    if native == "mismatch":
        return "lean_native_metric_mismatch"
    if readiness == "mismatch":
        return "production_readiness_mismatch"
    if inputs == "mismatch":
        return "compatibility_input_mismatch"
    if native != "match":
        return "lean_native_metric_parity_unavailable"
    if readiness != "match":
        return "production_readiness_parity_unavailable"
    if inputs != "match":
        return "compatibility_input_parity_unavailable"
    return "trade_reconciliation_diverged"


def read_native_metric_parity(lean_statistics_json: str | None) -> NativeMetricParityReceipt:
    """The LEAN-native metric-reproduction receipt the companion persisted under ``namespaces``."""
    if not lean_statistics_json or not lean_statistics_json.strip():
        return NativeMetricParityReceipt.unavailable("lean_statistics_missing")
    try:
        document = json.loads(lean_statistics_json)
    except json.JSONDecodeError:
        return NativeMetricParityReceipt.unavailable("lean_statistics_unreadable")
    receipt = document.get("namespaces") if isinstance(document, Mapping) else None
    if not isinstance(receipt, Mapping):
        return NativeMetricParityReceipt.unavailable("lean_native_metric_receipt_missing")
    divergences = receipt.get("divergences")
    return NativeMetricParityReceipt(
        status=_string(receipt, "status") or "unavailable",
        reason=_string(receipt, "reason"),
        contract_id=_string(receipt, "contract_id"),
        source_commit=_string(receipt, "source_commit"),
        absolute_tolerance=_number(receipt, "absolute_tolerance"),
        native_metric_count=_integer(receipt, "native_metric_count"),
        formatted_metric_count=_integer(receipt, "formatted_metric_count"),
        divergence_count=len(divergences) if isinstance(divergences, list) else 0,
    )


def compare_readiness(left_json: str | None, right_json: str | None) -> ReadinessParityReceipt:
    """Compare the two runs' production-readiness evidence through their parity signatures."""
    if not left_json or not right_json:
        return ReadinessParityReceipt.unavailable("run_verdict_missing")
    try:
        left, right = json.loads(left_json), json.loads(right_json)
    except json.JSONDecodeError:
        return ReadinessParityReceipt.unavailable("run_verdict_unreadable")
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return ReadinessParityReceipt.unavailable("run_verdict_unreadable")
    left_signature = left.get("parity_signature") if isinstance(left.get("parity_signature"), Mapping) else None
    right_signature = right.get("parity_signature") if isinstance(right.get("parity_signature"), Mapping) else None
    if (
        left_signature is not None
        and right_signature is not None
        and _string(left_signature, "contract_id") != _string(right_signature, "contract_id")
    ):
        # Both sides carry a signature, but from different contract versions. A raw
        # equality would report "mismatch" for a shape difference that says nothing
        # about evidence quality, so this is its own non-comparable outcome.
        return ReadinessParityReceipt.unavailable("readiness_contract_version_differs")
    if left_signature is not None or right_signature is not None:
        matches = left_signature is not None and right_signature is not None and left_signature == right_signature
        signature = right_signature if right_signature is not None else left_signature
        assert signature is not None
        return ReadinessParityReceipt(
            status="match" if matches else "mismatch",
            reason=None if matches else "readiness_signature_differs",
            compared_field_count=_integer(signature, "required_metrics"),
            mismatched_fields=() if matches else ("parity_signature",),
        )
    # Legacy verdicts predate the Python-authored, tolerance-pinned receipt.
    mismatches = tuple(field for field in READINESS_FIELDS if not _properties_equal(left, right, field))
    return ReadinessParityReceipt(
        status="match" if not mismatches else "mismatch",
        reason=None if not mismatches else "readiness_fields_differ",
        compared_field_count=len(READINESS_FIELDS),
        mismatched_fields=mismatches,
    )


def compare_inputs(left: RunDetail, right: RunDetail) -> InputParityReceipt:
    """Did both engines consume the same pinned data and compatibility settings?"""
    if not left.data_policy_json or not right.data_policy_json:
        return InputParityReceipt.unavailable("data_policy_missing")
    try:
        left_policy, right_policy = json.loads(left.data_policy_json), json.loads(right.data_policy_json)
    except json.JSONDecodeError:
        return InputParityReceipt.unavailable("data_policy_unreadable")
    if not isinstance(left_policy, Mapping) or not isinstance(right_policy, Mapping):
        return InputParityReceipt.unavailable("data_policy_unreadable")
    mismatches = [field for field in DATA_POLICY_FIELDS if not _properties_equal(left_policy, right_policy, field)]
    if left.initial_cash != right.initial_cash:
        mismatches.append("initial_cash")
    if left.start_ms != right.start_ms:  # both ET-midnight anchors: equal iff the trading dates are
        mismatches.append("start_date")
    if left.end_ms != right.end_ms:
        mismatches.append("end_date")
    if left.fill_mode != right.fill_mode:
        mismatches.append("fill_mode")
    return InputParityReceipt(
        status="match" if not mismatches else "mismatch",
        reason=None if not mismatches else "compatibility_inputs_differ",
        compared_field_count=len(DATA_POLICY_FIELDS) + 4,
        fixture_id=_string(right_policy, "fixture_id"),
        fixture_sha256=_string(right_policy, "fixture_sha256"),
        mismatched_fields=tuple(mismatches),
    )


def _properties_equal(left: Mapping[str, Any], right: Mapping[str, Any], name: str) -> bool:
    has_left, has_right = name in left, name in right
    return has_left == has_right and (not has_left or left[name] == right[name])


def _string(parent: Mapping[str, Any], name: str) -> str | None:
    value = parent.get(name)
    return value if isinstance(value, str) else None


def _number(parent: Mapping[str, Any], name: str) -> float | None:
    value = parent.get(name)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _integer(parent: Mapping[str, Any], name: str) -> int:
    value = parent.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def freeze_parity_for_lean_run(conn: asyncpg.Connection, *, right_run_id: int, parity_group_id: str) -> bool:
    """Compute and freeze the group's verdict once its LEAN companion is persisted.

    Returns whether a verdict was written. A group whose Python run cannot be
    found stays pending (the report shows the stall honestly); a group that
    already reached a terminal state is left alone.
    """
    right = await repo.get_run(conn, right_run_id, trade_limit=None)
    left_id = await repo.find_left_run_id(conn, parity_group_id)
    left = None if left_id is None else await repo.get_run(conn, left_id, trade_limit=None)
    if right is None or left is None:
        logger.warning(
            "[PARITY] Cannot compute verdict for group %s: left=%s right=%s",
            parity_group_id,
            left is not None,
            right is not None,
        )
        return False
    verdict = compute_parity_verdict(parity_group_id=parity_group_id, left=left, right=right)
    written = await repo.freeze_parity_verdict(
        conn,
        parity_group_id=parity_group_id,
        left_run_id=left.id,
        right_run_id=right.id,
        status=verdict.status,
        verdict_json=verdict.verdict_json,
    )
    if written:
        logger.info("[PARITY] Frozen verdict for group %s: %s", parity_group_id, verdict.status)
    else:
        logger.info("[PARITY] Verdict for group %s already terminal; not overwriting", parity_group_id)
    return written
