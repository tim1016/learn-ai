"""Freeze the cross-engine parity verdict when the LEAN companion lands (PRD #1929).

A port of the retired .NET ``ParityVerdictService``: the verdict compares
the Python engine run (left) with its LEAN companion (right) on five axes —
trade reconciliation (``lean_sidecar_compare_service.reconcile_trade_lists``,
the same classifier the .NET service reached over HTTP), the LEAN-native
metric-reproduction receipt persisted with the companion, the two runs'
production-readiness envelopes, and the compatibility inputs (data policy,
cash, window, fill mode), and the stored normalized resolved strategy
parameters. Nothing numerical is recomputed here; the receipts are verified,
not re-derived.

State machine: the **dispatch** path writes ``pending``, ``unavailable``, and
the provisional ``run_failed`` / ``persist_failed`` (the last two claim that no
comparable companion is coming). This module writes what a landed companion
row justifies — ``agree``, ``diverged``, ``unavailable``, or ``run_failed``
when the companion produced no comparable result — and that supersedes a
provisional claim. A verdict computed here, and the ``unavailable``
disposition of a group that never dispatched a companion, are never
overwritten (ADR 0058 as amended by #1977).

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

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs.records import trades_as_compare_payload
from app.research.backtest_runs.repository import RunDetail
from app.research.persistence.db import connection
from app.services.lean_sidecar_compare_service import reconcile_trade_lists
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# Version 3 adds the certificate-grade configuration receipt.  The JSON
# readers intentionally remain shape-tolerant below so verdicts frozen under
# earlier versions continue to render as their original historical evidence.
VERDICT_VERSION = 3
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
class ParameterParityReceipt:
    status: str
    reason: str | None
    compared_field_count: int
    mismatched_fields: tuple[str, ...]

    @classmethod
    def unavailable(cls, reason: str) -> ParameterParityReceipt:
        return cls("unavailable", reason, 0, ())


@dataclass(frozen=True, slots=True)
class ProgramVersionParityReceipt:
    status: str
    reason: str | None
    left_program_version: str | None
    right_program_version: str | None


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
    parameters = compare_parameters(left, right)
    program_version = compare_program_versions(left, right)
    status = _resolve_status(
        len(comparison.divergences),
        native.status,
        readiness.status,
        inputs.status,
        parameters.status,
        program_version.status,
    )
    reason = _resolve_reason(
        status,
        native.status,
        readiness.status,
        inputs.status,
        parameters.status,
        program_version.status,
    )
    counts: dict[str, int] = {}
    for divergence in comparison.divergences:
        counts[divergence.category] = counts.get(divergence.category, 0) + 1
    verdict = {
        "schema_version": VERDICT_VERSION,
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
        "parameter_parity": asdict(parameters),
        "program_version_parity": asdict(program_version),
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


def _resolve_status(
    trade_divergences: int,
    native: str,
    readiness: str,
    inputs: str,
    parameters: str,
    program_version: str,
) -> str:
    comparable_inputs = (inputs, parameters, program_version)
    # An observed trade difference is not a comparable deviation when a
    # required input/configuration receipt is absent. Keep that as unavailable
    # so only a conspicuous Manual override can promote it.
    if any(receipt != "match" for receipt in comparable_inputs):
        return "unavailable"
    result_receipts = (native, readiness)
    if any(receipt not in {"match", "mismatch"} for receipt in result_receipts):
        return "unavailable"
    if trade_divergences > 0 or "mismatch" in result_receipts:
        return "diverged"
    return "agree"


def _resolve_reason(
    status: str,
    native: str,
    readiness: str,
    inputs: str,
    parameters: str,
    program_version: str,
) -> str | None:
    if status == "agree":
        return None
    if inputs == "mismatch":
        return "compatibility_input_mismatch"
    if parameters == "mismatch":
        return "strategy_parameter_mismatch"
    if program_version == "mismatch":
        return "program_version_mismatch"
    if native not in {"match", "mismatch"}:
        return "lean_native_metric_parity_unavailable"
    if readiness not in {"match", "mismatch"}:
        return "production_readiness_parity_unavailable"
    if inputs not in {"match", "mismatch"}:
        return "compatibility_input_parity_unavailable"
    if parameters not in {"match", "mismatch"}:
        return "strategy_parameter_parity_unavailable"
    if program_version not in {"match", "mismatch"}:
        return "program_version_parity_unavailable"
    if native == "mismatch":
        return "lean_native_metric_mismatch"
    if readiness == "mismatch":
        return "production_readiness_mismatch"
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
    receipt_status = _string(receipt, "status")
    if receipt_status not in {"match", "mismatch"}:
        return NativeMetricParityReceipt.unavailable(
            _string(receipt, "reason") or "lean_native_metric_receipt_status_invalid"
        )
    contract_id = _nonblank_string(receipt, "contract_id")
    source_commit = _nonblank_string(receipt, "source_commit")
    absolute_tolerance = _number(receipt, "absolute_tolerance")
    native_metric_count = _integer(receipt, "native_metric_count")
    formatted_metric_count = _integer(receipt, "formatted_metric_count")
    if (
        contract_id is None
        or source_commit is None
        or absolute_tolerance is None
        or absolute_tolerance < 0
        or native_metric_count <= 0
        or formatted_metric_count <= 0
        or not isinstance(divergences, list)
        or (receipt_status == "match" and divergences)
        or (receipt_status == "mismatch" and not divergences)
    ):
        return NativeMetricParityReceipt.unavailable("lean_native_metric_receipt_incomplete")
    return NativeMetricParityReceipt(
        status=receipt_status,
        reason=_string(receipt, "reason"),
        contract_id=contract_id,
        source_commit=source_commit,
        absolute_tolerance=absolute_tolerance,
        native_metric_count=native_metric_count,
        formatted_metric_count=formatted_metric_count,
        divergence_count=len(divergences),
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
    if (left_signature is None) != (right_signature is None):
        return ReadinessParityReceipt.unavailable("readiness_signature_missing")
    if left_signature is not None and right_signature is not None and (
        not _readiness_signature_complete(left_signature)
        or not _readiness_signature_complete(right_signature)
    ):
        return ReadinessParityReceipt.unavailable("readiness_signature_incomplete")
    if left_signature is not None and right_signature is not None and (
        _string(left_signature, "contract_id") != _string(right_signature, "contract_id")
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
    if any(field not in left or field not in right for field in READINESS_FIELDS):
        return ReadinessParityReceipt.unavailable("readiness_fields_missing")
    mismatches = tuple(field for field in READINESS_FIELDS if not _properties_equal(left, right, field))
    return ReadinessParityReceipt(
        status="match" if not mismatches else "mismatch",
        reason=None if not mismatches else "readiness_fields_differ",
        compared_field_count=len(READINESS_FIELDS),
        mismatched_fields=mismatches,
    )


def _readiness_signature_complete(signature: Mapping[str, Any]) -> bool:
    required_fields = (
        "contract_id",
        "absolute_tolerance",
        "status",
        "available_required_metrics",
        "required_metrics",
        "missing_required_metrics",
        "required_inputs",
    )
    if any(field not in signature for field in required_fields):
        return False
    available = signature.get("available_required_metrics")
    required = signature.get("required_metrics")
    return (
        _nonblank_string(signature, "contract_id") is not None
        and _nonblank_string(signature, "absolute_tolerance") is not None
        and _nonblank_string(signature, "status") is not None
        and isinstance(available, int)
        and not isinstance(available, bool)
        and isinstance(required, int)
        and not isinstance(required, bool)
        and required > 0
        and 0 <= available <= required
        and isinstance(signature.get("missing_required_metrics"), list)
        and isinstance(signature.get("required_inputs"), list)
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
    try:
        left_execution = json.loads(left.execution_config_json or "")
        right_execution = json.loads(right.execution_config_json or "")
    except json.JSONDecodeError:
        return InputParityReceipt.unavailable("execution_configuration_unreadable")
    if not isinstance(left_execution, Mapping) or not isinstance(right_execution, Mapping):
        return InputParityReceipt.unavailable("execution_configuration_missing")
    left_fixture_id = _nonblank_string(left_policy, "fixture_id")
    right_fixture_id = _nonblank_string(right_policy, "fixture_id")
    left_fixture_sha256 = _nonblank_string(left_policy, "fixture_sha256")
    right_fixture_sha256 = _nonblank_string(right_policy, "fixture_sha256")
    if None in (left_fixture_id, right_fixture_id, left_fixture_sha256, right_fixture_sha256):
        return InputParityReceipt.unavailable("fixture_identity_missing")

    mismatches = [field for field in DATA_POLICY_FIELDS if not _properties_equal(left_policy, right_policy, field)]
    if left.initial_cash != right.initial_cash:
        mismatches.append("initial_cash")
    if left.start_ms != right.start_ms:  # both ET-midnight anchors: equal iff the trading dates are
        mismatches.append("start_date")
    if left.end_ms != right.end_ms:
        mismatches.append("end_date")
    if left.fill_mode != right.fill_mode:
        mismatches.append("fill_mode")
    if left_execution != right_execution:
        mismatches.append("execution_configuration")
    return InputParityReceipt(
        status="match" if not mismatches else "mismatch",
        reason=None if not mismatches else "compatibility_inputs_differ",
        compared_field_count=len(DATA_POLICY_FIELDS) + 5,
        fixture_id=right_fixture_id,
        fixture_sha256=right_fixture_sha256,
        mismatched_fields=tuple(mismatches),
    )


def compare_parameters(left: RunDetail, right: RunDetail) -> ParameterParityReceipt:
    """Compare the normalized, resolved parameters actually persisted for each run.

    ``record_from_payload`` writes the authoritative row symbol back into the
    parameter object after trimming and upper-casing it.  Normalize that one
    field in the same way for historical rows, but do not invent defaults or
    coerce other strategy values: absent or differently typed values are
    evidence that the two resolved configurations are not comparable.
    """
    left_parameters = _read_parameters(left.parameters_json)
    right_parameters = _read_parameters(right.parameters_json)
    if left_parameters is None or right_parameters is None:
        return ParameterParityReceipt.unavailable("strategy_parameters_unreadable")

    left_normalized = _normalize_parameters(left_parameters)
    right_normalized = _normalize_parameters(right_parameters)
    if left_normalized is None or right_normalized is None:
        return ParameterParityReceipt.unavailable("strategy_parameters_invalid")

    fields = tuple(sorted(set(left_normalized) | set(right_normalized)))
    mismatches = tuple(
        field
        for field in fields
        if field not in left_normalized
        or field not in right_normalized
        or left_normalized[field] != right_normalized[field]
    )
    return ParameterParityReceipt(
        status="match" if not mismatches else "mismatch",
        reason=None if not mismatches else "strategy_parameters_differ",
        compared_field_count=len(fields),
        mismatched_fields=mismatches,
    )


def compare_program_versions(left: RunDetail, right: RunDetail) -> ProgramVersionParityReceipt:
    """Require both engines to name the same persisted Signal Program version."""
    left_version = left.program_version.strip() if left.program_version else None
    right_version = right.program_version.strip() if right.program_version else None
    if left_version is None or right_version is None:
        return ProgramVersionParityReceipt("unavailable", "program_version_missing", left_version, right_version)
    matches = left_version == right_version
    return ProgramVersionParityReceipt(
        "match" if matches else "mismatch",
        None if matches else "program_versions_differ",
        left_version,
        right_version,
    )


def _read_parameters(parameters_json: str | None) -> Mapping[str, Any] | None:
    if not parameters_json or not parameters_json.strip():
        return None
    try:
        parameters = json.loads(parameters_json)
    except json.JSONDecodeError:
        return None
    return parameters if isinstance(parameters, Mapping) else None


def _normalize_parameters(parameters: Mapping[str, Any]) -> dict[str, Any] | None:
    normalized = dict(parameters)
    symbol = normalized.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    normalized["symbol"] = symbol.strip().upper()
    return normalized


def _properties_equal(left: Mapping[str, Any], right: Mapping[str, Any], name: str) -> bool:
    has_left, has_right = name in left, name in right
    return has_left == has_right and (not has_left or left[name] == right[name])


def _string(parent: Mapping[str, Any], name: str) -> str | None:
    value = parent.get(name)
    return value if isinstance(value, str) else None


def _nonblank_string(parent: Mapping[str, Any], name: str) -> str | None:
    value = _string(parent, name)
    return value if value is not None and value.strip() else None


def _number(parent: Mapping[str, Any], name: str) -> float | None:
    value = parent.get(name)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _integer(parent: Mapping[str, Any], name: str) -> int:
    value = parent.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def failed_companion_verdict(*, parity_group_id: str, detail: str) -> ParityVerdict:
    """The terminal verdict a companion that produced no comparable result justifies.

    Its row exists and carries the group, but a zero-trade failed run is not a
    comparison: reconciling it against a real Python run would report a
    divergence that did not happen. ``run_failed`` is the honest answer, and it
    is a verdict like any other so it travels the one freeze path (#1977).
    """
    return ParityVerdict(
        status="run_failed",
        verdict_json=json.dumps(
            {
                "schema_version": 1,
                "parity_group_id": parity_group_id,
                "status": "run_failed",
                "reason": detail,
                "computed_at_ms": now_ms_utc(),
            }
        ),
    )


async def settle_parity_for_lean_run(
    *, right_run_id: int, parity_group_id: str, failure_detail: str | None = None
) -> bool:
    """Settle the group from the LEAN companion row that just landed.

    Returns whether a verdict was written. A group whose Python run cannot be
    found stays pending (the report shows the stall honestly); a group that
    already carries a computed verdict, or the ``unavailable`` disposition of a
    group that never dispatched a companion, is left alone. A provisional
    dispatch failure is superseded — see :func:`repository.freeze_parity_verdict`.

    The three phases take their own connections rather than one spanning the
    whole call: the compare is pure CPU that would otherwise pin a pooled
    connection for its duration, and nothing here spans a transaction —
    ``freeze_parity_verdict`` opens its own (#1977).

    Load-bearing guard three modules away: a companion that failed and was then
    re-run under the *same* ``lean_run_id`` would have ``insert_run`` hand back
    the original zero-trade row, and this would compare it and manufacture a
    ``diverged`` over an honest ``run_failed``. It cannot happen because
    ``lean_sidecar_service`` refuses a ``run_id`` whose workspace still exists
    (``RunIdAlreadyUsedError``), and the LEAN backfill CLI never passes a
    ``parity_group_id`` at all. Relax either and this needs a freshness check.
    """
    async with connection() as conn:
        left_id = await repo.find_left_run_id(conn, parity_group_id)
        left = None if left_id is None else await repo.get_run(conn, left_id, trade_limit=None)
        # A failed companion is never compared, so its row is never read back.
        right = None if failure_detail is not None else await repo.get_run(conn, right_run_id, trade_limit=None)

    if left is None:
        logger.warning("[PARITY] Cannot settle group %s: it has no Python run", parity_group_id)
        return False
    if failure_detail is not None:
        verdict = failed_companion_verdict(parity_group_id=parity_group_id, detail=failure_detail)
    elif right is None:
        logger.warning("[PARITY] Cannot settle group %s: companion run %s not found", parity_group_id, right_run_id)
        return False
    else:
        verdict = compute_parity_verdict(parity_group_id=parity_group_id, left=left, right=right)

    async with connection() as conn:
        written = await repo.freeze_parity_verdict(
            conn,
            parity_group_id=parity_group_id,
            left_run_id=left.id,
            right_run_id=right_run_id,
            status=verdict.status,
            verdict_json=verdict.verdict_json,
        )
    if written:
        logger.info("[PARITY] Settled group %s: %s", parity_group_id, verdict.status)
    else:
        logger.info("[PARITY] Verdict for group %s is already computed; not overwriting", parity_group_id)
    return written
