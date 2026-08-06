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
ORDER_OUTCOME_UNKNOWN_REASON_CODE = "ORDER_OUTCOME_UNKNOWN"


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
        return {"positions": [position.to_mapping() for position in self.positions]}

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
        return {"identities": [identity.to_mapping() for identity in self.identities]}

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


def broker_snapshot_stale_cause_is_valid(value: Any) -> bool:
    return value == {"snapshot": "open_orders_and_positions"}


__all__ = [
    "BROKER_SNAPSHOT_STALE_REASON_CODE",
    "ORDER_OUTCOME_UNKNOWN_REASON_CODE",
    "POSITION_DRIFT_REASON_CODE",
    "OrderOutcomeUnknownCause",
    "PositionDriftCause",
    "PositionDriftObservation",
    "UnknownOrderIdentity",
    "broker_snapshot_stale_cause_is_valid",
]
