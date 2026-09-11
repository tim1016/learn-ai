"""The one validated type that constructs a ``LiveEnvelopeValues`` from storage.

Formula: none — this type carries no arithmetic. It carries the *domain* the
  six envelope values must satisfy and the *Python types* their canonical hash
  depends on.
Reference: ADR 0060 Decision 6; contract §2.4;
  ``docs/architecture/alpaca-configuration-ownership-inventory.md`` §A
  "Type-fidelity warning".
Canonical implementation: this file, for stored values. ``AlpacaSettings``
  (``app/broker/alpaca/config.py``) stays canonical for environment-sourced
  values; the two are pinned equal by
  ``tests/broker_configuration/test_envelope_type_fidelity.py``.
Validated against: that test — store → load → ``LiveEnvelopeValues.sha`` equals
  the sha of the same values built from ``AlpacaSettings``, and a historical
  arming seal still verifies over the reconstructed envelope.

Why the type check is by name and not by ``isinstance``: ``LiveEnvelopeValues``
is a frozen dataclass with no field validation, and its ``sha`` is
``canonical_sha256(asdict(self))`` — a ``json.dumps`` with no ``default=``.
So ``Decimal`` raises instead of hashing, ``5000`` and ``5000.0`` are different
documents, and ``True`` is an ``int`` that would seal as ``true``. The same
reasoning produced ``live_arming.py``'s ``_is_int``; the rule is restated here
because the profiles database is the second place that must not violate it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker_configuration.errors import InvalidLiveEnvelope

# ``(field, sqlite column affinity)`` in the order the contract's §2.4 table
# lists them. The affinities are asserted against the shipped DDL by
# ``tests/broker_configuration/test_envelope_type_fidelity.py``.
FLOAT_FIELDS: tuple[str, ...] = ("loss_fraction", "loss_usd", "xh_entry_bps", "xh_exit_bps")
INT_FIELDS: tuple[str, ...] = ("shadow_sessions", "arming_max_sessions")
ENVELOPE_FIELDS: tuple[str, ...] = (
    "loss_fraction",
    "loss_usd",
    "shadow_sessions",
    "arming_max_sessions",
    "xh_entry_bps",
    "xh_exit_bps",
)

_MAX_BPS = 10_000.0


def _as_float(field: str, value: Any) -> float:
    """Coerce exactly what ``AlpacaSettings``' ``float`` annotation would accept.

    ``int`` is admitted and widened, which is what a pydantic ``float`` field
    does with ``5000``; every other non-``float`` type — ``Decimal``, ``str``,
    ``bool`` — is refused rather than silently widened, because widening them
    is how a value that cannot hash reaches the envelope.
    """
    if type(value) is float:
        result = value
    elif type(value) is int:
        result = float(value)
    else:
        raise InvalidLiveEnvelope(
            f"{field} must be a float; {type(value).__name__} cannot seal into a live envelope.",
            next_step="Send the value as a JSON number.",
        )
    if not isfinite(result):
        raise InvalidLiveEnvelope(f"{field} must be a finite number.")
    return result


def _as_int(field: str, value: Any) -> int:
    """Require an ``int`` by name — ``True`` and ``1.0`` are both refused."""
    if type(value) is not int:
        raise InvalidLiveEnvelope(
            f"{field} must be a whole number of sessions; {type(value).__name__} is not.",
            next_step="Send the value as a JSON integer.",
        )
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidLiveEnvelope(message)


@dataclass(frozen=True)
class ValidatedLiveEnvelope:
    """The six values, in the domain and the Python types the seal depends on.

    Construct through :meth:`from_mapping`; the dataclass constructor validates
    too, so no path reaches ``LiveEnvelopeValues`` without passing here.
    """

    loss_fraction: float
    loss_usd: float
    shadow_sessions: int
    arming_max_sessions: int
    xh_entry_bps: float
    xh_exit_bps: float

    def __post_init__(self) -> None:
        for field in FLOAT_FIELDS:
            object.__setattr__(self, field, _as_float(field, getattr(self, field)))
        for field in INT_FIELDS:
            object.__setattr__(self, field, _as_int(field, getattr(self, field)))
        _require(0 < self.loss_fraction < 1, "loss_fraction must be greater than 0 and less than 1.")
        _require(self.loss_usd > 0, "loss_usd must be greater than 0.")
        _require(self.shadow_sessions >= 1, "shadow_sessions must be at least 1.")
        _require(self.arming_max_sessions >= 1, "arming_max_sessions must be at least 1.")
        _require(
            0 <= self.xh_entry_bps < _MAX_BPS,
            "xh_entry_bps must be at least 0 and less than 10000.",
        )
        _require(
            0 <= self.xh_exit_bps < _MAX_BPS,
            "xh_exit_bps must be at least 0 and less than 10000.",
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> ValidatedLiveEnvelope:
        """Build from stored or request data, refusing an unknown or missing key."""
        supplied = set(mapping)
        expected = set(ENVELOPE_FIELDS)
        missing = sorted(expected - supplied)
        if missing:
            raise InvalidLiveEnvelope(
                "The live envelope needs every value; missing: " + ", ".join(missing),
                next_step="Supply all six live envelope values, or save the revision as paper.",
            )
        unexpected = sorted(supplied - expected)
        if unexpected:
            raise InvalidLiveEnvelope(
                "The live envelope carries values it does not define: " + ", ".join(unexpected)
            )
        return cls(**{field: mapping[field] for field in ENVELOPE_FIELDS})

    def to_values(self) -> LiveEnvelopeValues:
        """The clerk's envelope dataclass, field-for-field — no rename layer."""
        return LiveEnvelopeValues(**{field: getattr(self, field) for field in ENVELOPE_FIELDS})

    def to_mapping(self) -> dict[str, float | int]:
        return {field: getattr(self, field) for field in ENVELOPE_FIELDS}

    @property
    def sha(self) -> str:
        """The envelope sha every arming record seals over."""
        return self.to_values().sha


__all__ = [
    "ENVELOPE_FIELDS",
    "FLOAT_FIELDS",
    "INT_FIELDS",
    "ValidatedLiveEnvelope",
]
