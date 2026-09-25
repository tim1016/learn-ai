"""The one validated type that constructs a ``LiveEnvelopeValues`` from storage.

Also home to its paper sibling, :class:`ValidatedPaperAllowances` — the two
extended-hours allowances a *paper* revision may carry without a live envelope
(#2440, owner decision 2026-09-25). The two share one statement of the bps
domain, so a paper allowance and a live one can never be bounded differently.

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
from app.broker_configuration.errors import (
    BrokerConfigurationError,
    InvalidLiveEnvelope,
    InvalidPaperAllowances,
)

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
# The two extended-hours allowances. A paper revision may carry these two on
# their own, named exactly as the envelope names them so a revision switched
# between modes keeps one meaning per key. Both or neither:
# ``ExtendedHoursAllowances`` has no one-sided form.
ALLOWANCE_FIELDS: tuple[str, ...] = ("xh_entry_bps", "xh_exit_bps")

_MAX_BPS = 10_000.0


def _as_float(field: str, value: Any, *, refusal: type[BrokerConfigurationError]) -> float:
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
        raise refusal(
            f"{field} must be a float; {type(value).__name__} cannot be saved as a number.",
            next_step="Send the value as a JSON number.",
        )
    if not isfinite(result):
        raise refusal(f"{field} must be a finite number.")
    return result


def _as_bps(field: str, value: Any, *, refusal: type[BrokerConfigurationError]) -> float:
    """An extended-hours allowance: a float in ``[0, 10000)`` bps.

    Upper-bounded because 10 000 bps is 100 %: a sell allowance at or past it
    floors the marketable anchor to zero or below, which is not a price. The
    one statement of the bound, for the envelope's pair and the paper pair.
    """
    result = _as_float(field, value, refusal=refusal)
    if not 0 <= result < _MAX_BPS:
        raise refusal(f"{field} must be at least 0 and less than 10000.")
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
            object.__setattr__(
                self, field, _as_float(field, getattr(self, field), refusal=InvalidLiveEnvelope)
            )
        for field in INT_FIELDS:
            object.__setattr__(self, field, _as_int(field, getattr(self, field)))
        _require(0 < self.loss_fraction < 1, "loss_fraction must be greater than 0 and less than 1.")
        _require(self.loss_usd > 0, "loss_usd must be greater than 0.")
        _require(self.shadow_sessions >= 1, "shadow_sessions must be at least 1.")
        _require(self.arming_max_sessions >= 1, "arming_max_sessions must be at least 1.")
        for field in ALLOWANCE_FIELDS:
            object.__setattr__(
                self, field, _as_bps(field, getattr(self, field), refusal=InvalidLiveEnvelope)
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


@dataclass(frozen=True)
class ValidatedPaperAllowances:
    """A paper revision's own extended-hours allowances, in bps (#2440).

    Owner decisions 2026-09-25: Start of a regular-hours run refuses until the
    account has an exit allowance, and so does a Resume of a flat run (a run
    still holding a position always resumes), because that run's EXIT on the
    day's last bar reaches the broker after the close as an after-hours limit
    priced off the decision bar's close. A live revision carries the pair
    inside its six-value envelope, sealed at arming; a paper revision has no
    envelope to carry it, so it carries these two and nothing else — never a
    loss limit or a session count, which bound real money only.

    No default, exactly like the envelope: ``None`` on the revision means "not
    set", which Start refuses; it is never read as zero.
    """

    xh_entry_bps: float
    xh_exit_bps: float

    def __post_init__(self) -> None:
        for field in ALLOWANCE_FIELDS:
            object.__setattr__(
                self, field, _as_bps(field, getattr(self, field), refusal=InvalidPaperAllowances)
            )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> ValidatedPaperAllowances:
        """Build from stored or request data, refusing an unknown or missing key.

        An unknown key is refused rather than dropped: a live-only value
        (``loss_usd``, a session count) offered here would otherwise be read as
        saved when it was not.
        """
        supplied = set(mapping)
        expected = set(ALLOWANCE_FIELDS)
        missing = sorted(expected - supplied)
        if missing:
            raise InvalidPaperAllowances(
                "The extended-hours allowances are set together; missing: " + ", ".join(missing),
                next_step="Set both the entry and the exit allowance, or neither.",
            )
        unexpected = sorted(supplied - expected)
        if unexpected:
            raise InvalidPaperAllowances(
                "A paper revision's allowances carry only xh_entry_bps and xh_exit_bps; "
                "it does not carry " + ", ".join(unexpected) + ".",
                next_step="Remove those values, or save the revision as live with its whole envelope.",
            )
        return cls(**{field: mapping[field] for field in ALLOWANCE_FIELDS})

    def to_mapping(self) -> dict[str, float]:
        return {field: getattr(self, field) for field in ALLOWANCE_FIELDS}


__all__ = [
    "ALLOWANCE_FIELDS",
    "ENVELOPE_FIELDS",
    "FLOAT_FIELDS",
    "INT_FIELDS",
    "ValidatedLiveEnvelope",
    "ValidatedPaperAllowances",
]
