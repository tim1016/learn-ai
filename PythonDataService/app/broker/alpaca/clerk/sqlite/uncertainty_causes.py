"""Strict, version-one cause payloads used by uncertainty authorization.

The operator-facing uncertainty envelope is intentionally descriptive.  These
small decoders are the separate authorization boundary: extra, missing, or
non-finite fields are rejected instead of being treated as a familiar cause.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

POSITION_DRIFT_REASON_CODE = "POSITION_DRIFT"
BROKER_SNAPSHOT_STALE_REASON_CODE = "BROKER_SNAPSHOT_STALE"
RECONCILIATION_INCOMPLETE_REASON_CODE = "RECONCILIATION_INCOMPLETE"
ORDER_OUTCOME_UNKNOWN_REASON_CODE = "ORDER_OUTCOME_UNKNOWN"
EXIT_NOT_FLAT_REASON_CODE = "EXIT_NOT_FLAT"
EXIT_STUCK_REASON_CODE = "EXIT_STUCK"
EXECUTION_COVERAGE_CONFLICT_REASON_CODE = "EXECUTION_COVERAGE_CONFLICT"
# The two former ``holds`` causes, folded into this registry by the v12
# migration (ADR 0048 Decision 2). Both are stored under the wire spelling the
# panel already publishes, so ``HOLD_REASON_BY_STORED_CODE``'s translation row
# for the old ``UNEXPLAINED_ORDER`` spelling retires with the migration.
UNEXPLAINED_ORDER_HOLD_REASON_CODE = "UNEXPLAINED_ORDER_HOLD"
STREAM_HEALTH_HOLD_REASON_CODE = "STREAM_HEALTH_HOLD"

# The reason codes whose episodes project as ``holds`` rather than
# ``uncertainties``. After v12 both live in one table, and this frozenset is
# the single partition between the two compatibility projections — every
# consumer that reads ``(*projection.holds, *projection.uncertainties)`` as a
# union depends on the two being disjoint, so a code may never appear in both.
# It lives in this leaf module so the reads and projections that partition on
# it need not import the policy registry that imports the repository.
HOLD_REASON_CODES: frozenset[str] = frozenset(
    {UNEXPLAINED_ORDER_HOLD_REASON_CODE, STREAM_HEALTH_HOLD_REASON_CODE}
)
# Rendered once for the ``IN (...)`` half of every partitioning query, so the
# SQL and the frozenset can never name different code sets.
HOLD_REASON_CODE_SQL_PLACEHOLDERS = ", ".join("?" * len(HOLD_REASON_CODES))
HOLD_REASON_CODE_SQL_PARAMS: tuple[str, ...] = tuple(sorted(HOLD_REASON_CODES))

# The stored spelling each hold cause had before v12, and the wire spelling it
# normalises to. ``STREAM_HEALTH_HOLD`` was already stored under its wire name
# and appears as an identity entry so this table states completely what a
# pre-v12 file or mirror may contain. Two callers share it — the v12 backfill
# and the legacy replay folds — and they must agree, or a migrated file and a
# replayed mirror of the same account would disagree on a reason code.
_HOLD_REASON_CODE_NORMALISATION: dict[str, str] = {
    "UNEXPLAINED_ORDER": UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE: UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE: STREAM_HEALTH_HOLD_REASON_CODE,
}


def normalized_hold_reason_code(stored_code: str) -> str:
    """The registered wire spelling for one stored hold reason code.

    Raises ``KeyError`` for anything else: an unregistered hold cause has no
    policy, and describing it as a generic uncertainty would silently drop the
    account-wide entry fence it was raised to hold.
    """
    try:
        return _HOLD_REASON_CODE_NORMALISATION[stored_code]
    except KeyError:
        raise KeyError(
            f"{stored_code!r} is not a known account-hold reason code"
        ) from None


def _require_exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError(f"cause keys must be exactly {sorted(expected)!r}")


def _finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


@dataclass(frozen=True)
class PositionDriftObservation:
    symbol: str
    broker_qty: float
    attributed_qty: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "broker_qty": self.broker_qty,
            "attributed_qty": self.attributed_qty,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> PositionDriftObservation:
        if not isinstance(value, dict):
            raise ValueError("position drift observation must be an object")
        _require_exact_keys(value, {"symbol", "broker_qty", "attributed_qty"})
        symbol = value["symbol"]
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise ValueError("position drift symbol must be a non-empty uppercase string")
        return cls(
            symbol=symbol,
            broker_qty=_finite_number(value["broker_qty"], field_name="broker_qty"),
            attributed_qty=_finite_number(value["attributed_qty"], field_name="attributed_qty"),
        )


@dataclass(frozen=True)
class PositionDriftCause:
    positions: tuple[PositionDriftObservation, ...]

    def to_mapping(self) -> dict[str, Any]:
        positions = sorted(self.positions, key=lambda position: position.symbol)
        return {"positions": [position.to_mapping() for position in positions]}

    @classmethod
    def from_mapping(cls, value: Any) -> PositionDriftCause:
        if not isinstance(value, dict):
            raise ValueError("position drift cause must be an object")
        _require_exact_keys(value, {"positions"})
        raw_positions = value["positions"]
        if not isinstance(raw_positions, list) or not raw_positions:
            raise ValueError("position drift cause must contain at least one position")
        positions = tuple(PositionDriftObservation.from_mapping(item) for item in raw_positions)
        symbols = [position.symbol for position in positions]
        if symbols != sorted(set(symbols)):
            raise ValueError("position drift observations must have unique sorted symbols")
        return cls(positions=positions)


@dataclass(frozen=True)
class ExitNotFlatCause:
    symbol: str
    attributed_qty: float

    def to_mapping(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "attributed_qty": self.attributed_qty}

    @classmethod
    def from_mapping(cls, value: Any) -> ExitNotFlatCause:
        if not isinstance(value, dict):
            raise ValueError("EXIT-not-flat cause must be an object")
        _require_exact_keys(value, {"symbol", "attributed_qty"})
        symbol = value["symbol"]
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise ValueError("EXIT-not-flat symbol must be a non-empty uppercase string")
        return cls(
            symbol=symbol,
            attributed_qty=_finite_number(
                value["attributed_qty"], field_name="attributed_qty"
            ),
        )


@dataclass(frozen=True)
class ExitStuckCause:
    """A stale EXIT_NOT_FLAT episode that exhausted automatic re-drives."""

    symbol: str
    attributed_qty: float
    redrive_count: int
    first_observed_at_ms: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "attributed_qty": self.attributed_qty,
            "redrive_count": self.redrive_count,
            "first_observed_at_ms": self.first_observed_at_ms,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> ExitStuckCause:
        if not isinstance(value, dict):
            raise ValueError("EXIT-stuck cause must be an object")
        _require_exact_keys(
            value, {"symbol", "attributed_qty", "redrive_count", "first_observed_at_ms"}
        )
        symbol = value["symbol"]
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise ValueError("EXIT-stuck symbol must be a non-empty uppercase string")
        redrive_count = value["redrive_count"]
        if not isinstance(redrive_count, int) or isinstance(redrive_count, bool) or redrive_count < 0:
            raise ValueError("EXIT-stuck redrive_count must be a non-negative integer")
        first_observed_at_ms = value["first_observed_at_ms"]
        if (
            not isinstance(first_observed_at_ms, int)
            or isinstance(first_observed_at_ms, bool)
            or first_observed_at_ms < 0
        ):
            raise ValueError("EXIT-stuck first_observed_at_ms must be int64 ms UTC")
        return cls(
            symbol=symbol,
            attributed_qty=_finite_number(value["attributed_qty"], field_name="attributed_qty"),
            redrive_count=redrive_count,
            first_observed_at_ms=first_observed_at_ms,
        )


@dataclass(frozen=True)
class ExecutionCoverageConflictCause:
    """Exact execution that cannot be safely merged with aggregate recovery."""

    order_ref: str
    execution_id: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "order_ref": self.order_ref,
            "execution_id": self.execution_id,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> ExecutionCoverageConflictCause:
        if not isinstance(value, dict):
            raise ValueError("execution coverage conflict cause must be an object")
        _require_exact_keys(value, {"order_ref", "execution_id"})
        if not all(
            isinstance(value[field], str) and value[field]
            for field in ("order_ref", "execution_id")
        ):
            raise ValueError("execution coverage conflict fields must be non-empty strings")
        return cls(order_ref=value["order_ref"], execution_id=value["execution_id"])


@dataclass(frozen=True)
class UnknownOrderIdentity:
    effect_operation_id: str
    order_ref: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "effect_operation_id": self.effect_operation_id,
            "order_ref": self.order_ref,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> UnknownOrderIdentity:
        if not isinstance(value, dict):
            raise ValueError("unknown-order identity must be an object")
        _require_exact_keys(value, {"effect_operation_id", "order_ref"})
        if not all(isinstance(value[field], str) and value[field] for field in ("effect_operation_id", "order_ref")):
            raise ValueError("unknown-order identity fields must be non-empty strings")
        return cls(
            effect_operation_id=value["effect_operation_id"],
            order_ref=value["order_ref"],
        )


@dataclass(frozen=True)
class OrderOutcomeUnknownCause:
    identities: tuple[UnknownOrderIdentity, ...]

    def to_mapping(self) -> dict[str, Any]:
        identities = sorted(
            self.identities,
            key=lambda identity: (identity.effect_operation_id, identity.order_ref),
        )
        return {"identities": [identity.to_mapping() for identity in identities]}

    @classmethod
    def from_mapping(cls, value: Any) -> OrderOutcomeUnknownCause:
        if not isinstance(value, dict):
            raise ValueError("unknown-order cause must be an object")
        _require_exact_keys(value, {"identities"})
        raw_identities = value["identities"]
        if not isinstance(raw_identities, list) or not raw_identities:
            raise ValueError("unknown-order cause must contain at least one identity")
        identities = tuple(UnknownOrderIdentity.from_mapping(identity) for identity in raw_identities)
        sort_keys = [(identity.effect_operation_id, identity.order_ref) for identity in identities]
        if sort_keys != sorted(set(sort_keys)):
            raise ValueError("unknown-order identities must be unique and sorted")
        return cls(identities=identities)


@dataclass(frozen=True)
class UnexplainedOrderCause:
    """Every foreign broker order still holding the account against entries.

    ``broker_order_ids`` are broker-assigned ids, never ``client_order_id`` —
    a genuinely foreign order may carry no client id at all (the constraint
    ``AccountHoldRaisedFacts`` documented before this cause was typed).
    """

    broker_order_ids: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {"broker_order_ids": sorted(self.broker_order_ids)}

    @classmethod
    def from_mapping(cls, value: Any) -> UnexplainedOrderCause:
        return cls(broker_order_ids=_ordered_evidence_strings(value, field_name="broker_order_ids"))


@dataclass(frozen=True)
class StreamHealthHoldCause:
    """The unhealthy channels that froze the account.

    Each entry is one channel's ``"{stream}: {reason}"`` evidence line, which
    is why this decoder validates shape rather than parsing structure — the
    reason half is provider prose, not a closed vocabulary.
    """

    channels: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {"channels": sorted(self.channels)}

    @classmethod
    def from_mapping(cls, value: Any) -> StreamHealthHoldCause:
        return cls(channels=_ordered_evidence_strings(value, field_name="channels"))


def _ordered_evidence_strings(value: Any, *, field_name: str) -> tuple[str, ...]:
    """Decode one uniquely-sorted list of non-empty strings.

    Shared by both hold causes: each is a bare evidence list, and the sorted
    uniqueness is what makes the stored envelope a stable identity for
    ``observe_uncertainty``'s append-on-change-only gate.

    An *empty* list is accepted, unlike the other causes in this module. For a
    hold, "no evidence remains" is not a malformed cause — it is precisely the
    resolved state, which both hold producers write verbatim
    (``AccountHoldResolvedFacts(evidence_refs=[])``). Rejecting it would make
    a resolved episode's own envelope undecodable and leave the v12 migration
    unable to carry resolved holds across, which is the timeline evidence the
    migration exists to preserve. No fence weakens: an episode blocks entries
    through its stored ``blocks_new_exposure`` column, and both hold policies
    declare ``allows_reduction=False``, so cause validity authorizes nothing
    for them in either direction.
    """
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} cause must be an object")
    _require_exact_keys(value, {field_name})
    raw = value[field_name]
    if not isinstance(raw, list):
        raise ValueError(f"{field_name} must be a list")
    if not all(isinstance(entry, str) and entry for entry in raw):
        raise ValueError(f"{field_name} entries must be non-empty strings")
    if raw != sorted(set(raw)):
        raise ValueError(f"{field_name} entries must be unique and sorted")
    return tuple(raw)


def broker_snapshot_stale_cause_is_valid(value: Any) -> bool:
    return value == {"snapshot": "open_orders_and_positions"}


def reconciliation_incomplete_cause_is_valid(value: Any) -> bool:
    return value == {"pass": "account_reconciliation"}


__all__ = [
    "BROKER_SNAPSHOT_STALE_REASON_CODE",
    "EXECUTION_COVERAGE_CONFLICT_REASON_CODE",
    "EXIT_NOT_FLAT_REASON_CODE",
    "EXIT_STUCK_REASON_CODE",
    "HOLD_REASON_CODES",
    "HOLD_REASON_CODE_SQL_PARAMS",
    "HOLD_REASON_CODE_SQL_PLACEHOLDERS",
    "ORDER_OUTCOME_UNKNOWN_REASON_CODE",
    "POSITION_DRIFT_REASON_CODE",
    "RECONCILIATION_INCOMPLETE_REASON_CODE",
    "STREAM_HEALTH_HOLD_REASON_CODE",
    "UNEXPLAINED_ORDER_HOLD_REASON_CODE",
    "ExecutionCoverageConflictCause",
    "ExitNotFlatCause",
    "ExitStuckCause",
    "OrderOutcomeUnknownCause",
    "PositionDriftCause",
    "PositionDriftObservation",
    "StreamHealthHoldCause",
    "UnexplainedOrderCause",
    "UnknownOrderIdentity",
    "broker_snapshot_stale_cause_is_valid",
    "normalized_hold_reason_code",
    "reconciliation_incomplete_cause_is_valid",
]
