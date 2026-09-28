"""The ADR 0059 risk envelope, as pure values and rules (Decision 4).

Formula: cash bound admits iff ``notional + reserved <= cash_available``;
  loss limit ``L = min(loss_fraction × last_equity, loss_usd)``; loss breached
  iff ``day_pnl <= −L``.
Reference: ADR 0059 Decision 4; owner rulings 2026-09-08 (plan R1–R4).
Canonical implementation: this file. Day P&L composition lives in
  ``app/broker/alpaca/clerk/sqlite/day_pnl.py``.
Validated against: ``tests/broker/alpaca/clerk/test_live_envelope.py``.

Nothing here touches a broker, a database, or a clock: the sync publishes
an ``AccountObservation`` it stamped with the repository clock, and the
admission seam asks the gate for one that is fresh at *its* ``now_ms``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from app.broker.alpaca.clerk.money import cash_admits, notional
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256

# ``uncertainty_causes`` owns the loss-hold reason code -- it is the module
# that declares every cause the Clerk can record, and it imports nothing from
# the Clerk itself. It is re-exported below so the admission set can be stated
# once, here, beside the three refusals this module does own.
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
)
from app.broker.contract.models import BrokerActivity

if TYPE_CHECKING:
    from app.broker.alpaca.config import AlpacaSettings

LIVE_ENVELOPE_CASH_EXCEEDED = "LIVE_ENVELOPE_CASH_EXCEEDED"
LIVE_ENVELOPE_DISAGREEMENT = "LIVE_ENVELOPE_DISAGREEMENT"
# The composition refusal, defined here with the three admission codes so the
# arming ceremony imports it rather than repeating the literal (ADR 0059 D4).
LIVE_ENVELOPE_MISSING = "LIVE_ENVELOPE_MISSING"
LIVE_ENVELOPE_UNOBSERVED = "LIVE_ENVELOPE_UNOBSERVED"
ENVELOPE_ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        LIVE_ENVELOPE_CASH_EXCEEDED,
        LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        LIVE_ENVELOPE_DISAGREEMENT,
        LIVE_ENVELOPE_UNOBSERVED,
    }
)
ENVELOPE_SYNC_INTERVAL_S = 15.0
# Three sync intervals: one missed tick is a blip, two is an outage the
# admission seam must not trade through.
OBSERVATION_MAX_AGE_MS = 45_000
# How long before an observation's reads were issued a fill the Clerk recorded
# is still not trusted to be in the broker's cash (#2441). The observation is
# stamped the instant its reads are issued, and the broker's answer is at
# least that recent -- but a fill recorded just before that instant is in the
# answer only if Alpaca's account ledger had caught up with a trade update
# Alpaca had already delivered. Alpaca documents no such ordering:
# account values are "updated Real-Time post trade executions" (Broker API
# FAQ), and neither the account endpoint nor the ``trade_updates`` stream
# states an ordering or consistency guarantee between the two
# (docs/references/alpaca-live-envelope.md cites each page). So a fill recorded
# within this margin before the stamp stays reserved, even though the cash may
# already include it (``AccountObservation.fills_seen_before_ms``):
# over-reserving refuses an ENTER that would have fit, under-reserving admits a
# second ENTER against cash already spent.
#
# Five seconds, confirmed by measurement (#2487): on the paper account's
# BTC/USD fills (2026-09-28, 45 fill events plus three rehearsal runs, ~150
# observations) the account cash never lagged a fill's trade update by more
# than a read's own round trip. In the committed run, 2 fills' cash was
# provably visible before the event; for the other 42 the first reflecting
# read answered at most 329 ms after the event (visibility at the receipt
# instant is interval-censored, not zero), and the worst bound across every
# run was 401 ms -- itself read-cadence quantization (reads p95 438 ms
# apart). Every bound is an answer-time bound: the sample establishes no
# issue-to-snapshot ordering guarantee, only that each fill's cash was
# provably visible within one read round trip of its event (fixture:
# ``tests/fixtures/alpaca/fill_visibility/paper-btcusd-2026-09-28.json``).
# The margin is kept at 5 s rather than tightened because that sample is
# crypto-only, weekend, one idle account: the equity engine the envelope
# actually gates is unmeasured, Alpaca publishes no ordering promise, and
# the cost of the margin is bounded by one 15 s observation of
# over-reservation. Pinned below the interval -- and above the measured
# maximum plus its documented cushion factor -- by
# ``tests/broker/alpaca/clerk/test_live_envelope.py``.
FILL_VISIBILITY_GRACE_MS = 5_000

EnvelopeAgreement = Literal["unsealed", "agreed", "disagreed"]

# (envelope field name, ``AlpacaSettings`` field name). Public because the
# profile resolver (``app/broker/alpaca/profile/runtime_context.py``) builds
# settings from stored envelope values through this same pairing, so the two
# directions cannot drift into separate rename layers (contract §2.4).
ENVELOPE_SETTINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("loss_fraction", "live_loss_fraction"),
    ("loss_usd", "live_loss_usd"),
    ("shadow_sessions", "live_shadow_sessions"),
    ("arming_max_sessions", "live_arming_max_sessions"),
    ("xh_entry_bps", "live_xh_entry_bps"),
    ("xh_exit_bps", "live_xh_exit_bps"),
)
CURRENT_ENVELOPE_SETTINGS_FIELDS: tuple[tuple[str, str], ...] = tuple(
    pair for pair in ENVELOPE_SETTINGS_FIELDS if pair[0] not in {"shadow_sessions", "arming_max_sessions"}
)

# The domain of each envelope value, as one table.
#
# Canonical declaration: ``AlpacaSettings`` in ``app/broker/alpaca/config.py``,
# whose pydantic ``Field`` constraints refuse to *boot* outside these. This is
# the second copy, and it exists for a real reason: an envelope also arrives
# from an arming record read off disk, which never passes through settings at
# all, and those values bound real money just as hard -- a sealed ``inf`` loss
# limit is never breached, a sealed 10 000 bps exit anchor floors to zero and
# refuses the leg. ``LiveEnvelopeValues`` is a plain frozen dataclass, so the
# check has to live somewhere it sees *both* producers.
# Validated against: ``tests/broker/alpaca/test_config.py::
# test_the_envelope_domains_agree_with_the_settings_that_declare_them`` -- the
# parity test that fails if either copy moves.
#
# Predicates rather than bounds because the two ends differ per field
# (``loss_fraction`` is exclusive at both, ``*_bps`` inclusive at zero), and
# because a comparison against a NaN or a non-number is False or a TypeError
# either way -- which is the answer both cases deserve.
_ENVELOPE_DOMAINS: tuple[tuple[str, Callable[[Any], bool], str], ...] = (
    ("loss_fraction", lambda value: 0 < value < 1, "in (0, 1)"),
    # Excludes NaN and infinity as well as zero: ``inf > 0`` is True.
    ("loss_usd", lambda value: 0 < value < float("inf"), "positive and finite"),
    ("shadow_sessions", lambda value: value >= 1, "at least 1"),
    ("arming_max_sessions", lambda value: value >= 1, "at least 1"),
    ("xh_entry_bps", lambda value: 0 <= value < 10_000, "in [0, 10000)"),
    ("xh_exit_bps", lambda value: 0 <= value < 10_000, "in [0, 10000)"),
)


class LiveEnvelopeIncomplete(ValueError):
    """A live envelope cannot be built: at least one ``ALPACA_LIVE_*`` value is absent."""


@dataclass(frozen=True)
class LiveEnvelopeValues:
    loss_fraction: float
    loss_usd: float
    xh_entry_bps: float
    xh_exit_bps: float
    # Historical records retain their original six-field seal. Current
    # Configuration has no session-count or expiring-grant authority.
    shadow_sessions: int | None = None
    arming_max_sessions: int | None = None

    @classmethod
    def from_settings(cls, settings: AlpacaSettings) -> LiveEnvelopeValues:
        missing = [name for _, name in CURRENT_ENVELOPE_SETTINGS_FIELDS if getattr(settings, name) is None]
        if missing:
            raise LiveEnvelopeIncomplete(
                "the live envelope needs every ALPACA_LIVE_* value; missing: " + ", ".join(missing)
            )
        return cls(**{field: getattr(settings, name) for field, name in CURRENT_ENVELOPE_SETTINGS_FIELDS})

    def to_mapping(self) -> dict[str, float | int]:
        return {name: value for name, value in asdict(self).items() if value is not None}

    @property
    def sha(self) -> str:
        return canonical_sha256(self.to_mapping())


def envelope_domain_violation(values: LiveEnvelopeValues) -> str | None:
    """The first value outside its domain, named with the domain it missed.

    A string rather than an exception so each caller phrases its own refusal:
    the settings path never reaches here (pydantic refuses the boot), and the
    arming path has to say *which record* carries the bad value.
    """
    for name, admits, domain in _ENVELOPE_DOMAINS:
        if name in {"shadow_sessions", "arming_max_sessions"} and getattr(values, name) is None:
            continue
        if not admits(getattr(values, name)):
            return f"{name} is not {domain}"
    return None


def envelope_agreement(
    configured: LiveEnvelopeValues, sealed: LiveEnvelopeValues | None
) -> EnvelopeAgreement:
    if sealed is None:
        return "unsealed"
    return "agreed" if sealed.sha == configured.sha else "disagreed"


@dataclass(frozen=True)
class AccountObservation:
    """One broker read the sync published, stamped with the repository clock.

    ``observed_at_ms`` is the instant the reads were *issued*, not the instant
    they returned: the broker's answer is only known to be at least that
    recent. Which fills its cash is trusted to include is
    :attr:`fills_seen_before_ms`. Freshness is aged from the same instant,
    which errs old by the read's round trip.
    """

    observed_at_ms: int
    broker_cash_usd: float
    # ``broker_cash_usd`` less what the Clerk's own fills would have spent
    # under simulated custody (plan R2); equal to it under real custody.
    cash_available_usd: float | Decimal
    last_equity_usd: float | None
    position_count: int | None
    equity_usd: float | None = None
    unrealized_pl_usd: float = 0.0  # simulation diagnostic; never the loss authority
    risk_cash_flows: tuple[BrokerActivity, ...] = ()
    risk_cash_flow_evidence_complete: bool = False
    risk_cash_flow_window_start_ms: int | None = None
    risk_equity_window_start_ms: int | None = None
    risk_revision: int | None = None
    # Effective fill watermark before requesting broker unrealized P&L. A
    # subsequent execution can close a lot already included in that mark.
    risk_fill_sequence: int = 0
    # Simulated cash is projected atomically from custody, including settled
    # modelled fees. These explicit cutoffs prevent subtracting them twice.
    simulation_cash_seen_before_ms: int | None = None
    modelled_fees_seen_before_ms: int | None = None
    simulation_session_start_ms: int | None = None
    simulation_marks_valid_until_ms: int | None = None

    @property
    def fills_seen_before_ms(self) -> int:
        """The instant before which a fill is assumed to be in this observation's cash.

        A fill the Clerk recorded strictly before this instant is assumed
        reflected in ``broker_cash_usd``; one recorded at it or later stays
        reserved. It sits ``FILL_VISIBILITY_GRACE_MS`` before the reads were
        issued, because a fill recorded just before them is not trusted to be
        in the answer either (#2441). Over-reserving is the safe side: it
        refuses an ENTER that would have fit, where under-reserving admits one
        against cash already spent.
        """
        return self.simulation_cash_seen_before_ms if self.simulation_cash_seen_before_ms is not None else self.observed_at_ms - FILL_VISIBILITY_GRACE_MS


@dataclass(frozen=True)
class EnvelopeReservation:
    """The cash one accepted ENTER claims until its fills are observed."""

    quantity: float
    reference_price: float
    exact_reference_price: str | None = None
    fee_provision_cents: int = 0

    @property
    def notional_usd(self) -> float:
        return float(notional(self.quantity, self.exact_reference_price or self.reference_price))


def observation_is_fresh(observation: AccountObservation, *, now_ms: int, max_age_ms: int) -> bool:
    """The one freshness rule for a published or a displayed observation.

    Fresh means ``0 <= age <= max_age``: an observation dated *after* the
    clock is not fresh either, so a backward clock step cannot make obsolete
    cash look current for the whole rollback.
    """
    return 0 <= now_ms - observation.observed_at_ms <= max_age_ms


def loss_limit_usd(values: LiveEnvelopeValues, *, last_equity_usd: float) -> float:
    return min(values.loss_fraction * last_equity_usd, values.loss_usd)


def loss_breached(*, day_pnl_usd: float, loss_limit_usd: float) -> bool:
    return day_pnl_usd <= -loss_limit_usd


def cash_bound_admits(
    *, cash_available_usd: float, reserved_usd: float, notional_usd: float
) -> bool:
    return cash_admits(cash=cash_available_usd, claims=reserved_usd, required=notional_usd)


class LiveEnvelopeGate:
    """The envelope one authority admits ENTERs against.

    Holds the configured values, the sealed values once an arming record
    exists (slice 6), and the latest observation the sync published. A
    process-local cache of a broker read, never a custody fact (the
    ``_last_published`` precedent in ``runtime.py``).
    """

    def __init__(
        self,
        *,
        values: LiveEnvelopeValues | None,
        sealed: LiveEnvelopeValues | None = None,
        custody_is_simulated: bool,
        observation_max_age_ms: int = OBSERVATION_MAX_AGE_MS,
    ) -> None:
        self.values = values
        self.sealed = sealed
        self.custody_is_simulated = custody_is_simulated
        self._max_age_ms = observation_max_age_ms
        self._observation: AccountObservation | None = None

    @property
    def agreement(self) -> EnvelopeAgreement:
        return "unsealed" if self.values is None else envelope_agreement(self.values, self.sealed)

    @property
    def in_force(self) -> LiveEnvelopeValues:
        """The values every judgement is made against: the sealed ones, else configured.

        ADR 0059 D3 seals every value at arming, so an edit to the environment
        is a re-arm and never a silent drift. The loss limit a hold is raised on
        and cleared against, and the allowance an extended-session leg is priced
        from, are therefore the newest arming record's -- not whatever the
        process happened to boot with. ``values`` is the fallback for an account
        no ceremony has ever armed, which has nothing sealed to prefer.

        **The fallback is a relaxation when the seal is merely unreadable**, and
        the caller owns that: ``sealed`` also returns to ``None`` when the
        arming inputs cannot be read, so a caller who must not relax has to ask
        whether they were. The loss judgement does
        (``sqlite/live_envelope_sync.LiveEnvelopeSync._seal_unreadable`` makes
        that account unjudgeable); extended-hours leg pricing deliberately does
        not, because an EXIT must never be refused for want of a seal.

        ``values`` remains the right input for the two questions that are
        *about* the configured half: ``agreement`` (does the environment still
        match what was armed?) and the arming snapshot's own disagreement check.
        """
        values = self.values if self.sealed is None else self.sealed
        if values is None:
            raise LiveEnvelopeIncomplete("No historical live envelope is configured")
        return values

    @property
    def in_force_is_sealed(self) -> bool:
        """Whether :attr:`in_force` answered a seal rather than the environment.

        The label half of the same question, so an operator surface naming
        which envelope decided does not restate the predicate.
        """
        return self.sealed is not None

    def publish(self, observation: AccountObservation) -> None:
        self._observation = observation

    def withdraw(self) -> None:
        """Drop the published observation.

        The sync could not judge the account, so ENTER refuses
        ``LIVE_ENVELOPE_UNOBSERVED`` at once (plan R3, R5) rather than
        bounding one against a figure that only *looks* fresh.
        """
        self._observation = None

    @property
    def observation_max_age_ms(self) -> int:
        return self._max_age_ms

    def latest_observation(self) -> AccountObservation | None:
        return self._observation

    def fresh_observation(self, now_ms: int) -> AccountObservation | None:
        """The published observation, if its age at ``now_ms`` is in range.

        Fresh means ``0 <= age <= max_age``: an observation dated *after* the
        admission clock is not fresh either. A backward clock step would
        otherwise make a negative age look fresh indefinitely, and ENTERs
        would keep bounding against obsolete cash for the whole rollback.
        """
        observation = self._observation
        if observation is None or not observation_is_fresh(observation, now_ms=now_ms, max_age_ms=self._max_age_ms):
            return None
        return observation


__all__ = [
    "ENVELOPE_ADMISSION_REASON_CODES",
    "ENVELOPE_SETTINGS_FIELDS",
    "ENVELOPE_SYNC_INTERVAL_S",
    "FILL_VISIBILITY_GRACE_MS",
    "LIVE_ENVELOPE_CASH_EXCEEDED",
    "LIVE_ENVELOPE_DISAGREEMENT",
    "LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE",
    "LIVE_ENVELOPE_MISSING",
    "LIVE_ENVELOPE_UNOBSERVED",
    "OBSERVATION_MAX_AGE_MS",
    "AccountObservation",
    "EnvelopeAgreement",
    "EnvelopeReservation",
    "LiveEnvelopeGate",
    "LiveEnvelopeIncomplete",
    "LiveEnvelopeValues",
    "cash_bound_admits",
    "envelope_agreement",
    "loss_breached",
    "loss_limit_usd",
    "observation_is_fresh",
]
