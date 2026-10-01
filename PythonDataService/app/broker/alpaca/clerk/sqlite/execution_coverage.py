"""Closed proof vocabulary for exact executions that overlap aggregate recovery.

Formula: Q = fsum(qty); C = fsum(qty × price); P = C / Q. The canonical set
  proof accepts only abs(Q_E - Q_R) < 1e-9 shares and
  abs(C_E - C_R) < tick(C_O / Q_O) × Q_O + max(|P_E|, |P_R|) × abs(Q_E - Q_R)
  currency, where Q_O and C_O are the order's effective fills and tick is
  one valid price increment (total_price_conflict_atol): putting the exacts
  in place of the cumulative moves the order's average price by less than
  one increment, beyond the priced share residue the quantity rule accepts.
Reference: Project-authored execution-coverage contract in PRD #1543, stories
  18, 19, 28, and 33; this is authored project logic, not a reused proof.
  The price rule is ADR 0036's vendor-rounding rule (2026-09-30 amendment,
  item 3), applied to coverage on 2026-10-01 (#2791).
Canonical implementation: this file's prove_execution_coverage_set. The
  direct exact-to-one-cumulative replacement is consumed by the #1554 Clerk
  fold; the existing exact_replaces_cumulative remains the temporary S0
  operator proof.
Validated against: tests/broker/alpaca/clerk/sqlite/test_execution_coverage_set_proof.py

The existing typed query below continues to describe the shipped S0 operator
flow. The direct automatic fold reuses this proof without changing that
operator path. Accumulated quarantined-exact reconciliation remains a later
slice, so keeping both vocabularies together preserves the temporary boundary
for audit.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCoverageQuarantinedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
    UncertaintyResolvedFacts,
    validate_execution_coverage_quarantined_facts,
    validate_execution_slice_facts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.order_projection import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.alpaca.marketable_limit import price_increment

#: Numerical-rigor tolerance for an exact slice that replaces an aggregate
#: recovery row. See ADR 0036, 2026-09-30 amendment, item 1.
FILL_QTY_EPSILON = 1e-9

#: Set-proof quantity tolerance in shares. The comparison is intentionally strict.
QTY_ATOL = 1e-9

#: The per-share price basis of the strict envelope coverage was proven with
#: before #2791 (:func:`strict_gross_cost_envelope`), kept so those records replay.
PRICE_ATOL = 1e-9


def total_price_conflict_atol(reported_avg_price: float) -> float:
    """One valid price increment at a reported average, per share (#2460, #2770).

    Formula: ``price_increment(reported_avg_price)``, ``rtol=0`` -- $0.01 at or
      above $1, $0.0001 below. A same-quantity average-price difference below
      one valid increment of the reported price cannot be told from vendor
      rounding; one at or above it is a real economic disagreement. A broker
      total that disagrees with its recorded fills by that much is recorded
      as ``EXECUTION_PRICE_CONFLICT``; exact executions that would move their
      order's average by that much cannot replace its cumulative (#2791).
    Assumes Alpaca reports a sub-dollar ``filled_avg_price`` to at least
      $0.0001; a coarser report would read its own rounding as a conflict.
    Reference: ADR 0036, 2026-09-30 amendment, item 3; the tick rule is
      Alpaca's price precision (``app/broker/alpaca/marketable_limit.py``).
    Canonical implementation: this function, on
      ``marketable_limit.price_increment``.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_economic_projection.py::
      test_a_sub_cent_difference_below_one_dollar_is_a_price_conflict,
      test_a_vendor_rounding_sized_difference_raises_no_conflict and
      test_the_sweep_keeps_a_sub_dollar_conflict_its_recorded_fills_still_disagree_with;
      tests/broker/alpaca/clerk/sqlite/test_execution_coverage_set_proof.py.
    """
    return float(price_increment(Decimal(str(reported_avg_price))))


@dataclass(frozen=True)
class ExecutionCoverageIdentity:
    """The one authority and economic identity a proof may compare."""

    account_id: str
    authority_generation: int
    database_identity_token: str
    order_ref: str
    symbol: str
    side: str


@dataclass(frozen=True)
class CumulativeCoverageObservation:
    """One current cumulative-recovery row, named by immutable source identity."""

    source_id: str
    identity: ExecutionCoverageIdentity
    quantity: float
    price: float


@dataclass(frozen=True)
class ExactCoverageObservation:
    """One quarantined or incoming exact execution, retaining its exact fee."""

    source_id: str
    identity: ExecutionCoverageIdentity
    quantity: float
    price: float
    fee: float | None


@dataclass(frozen=True)
class ExecutionCoverageExactProvenance:
    """One prior quarantined exact's immutable custody observation clocks.

    The automatic supersession fold restores the effective fill from this
    record rather than assigning the later resolving transition's clocks. The
    two sequence fields intentionally agree: one names the custody transition
    that quarantined the source, while the other is persisted on the rebuilt
    fill for deterministic FIFO ordering when broker event timestamps tie.
    """

    exact_execution: ExecutionSliceFilledFacts
    observation_transition_sequence: int
    clerk_observed_at_ms: int
    recorded_at_ms: int
    recorded_transition_sequence: int


@dataclass(frozen=True)
class ExecutionCoverageSupersededFacts:
    """Automatic replacement of cumulative coverage by exact evidence.

    The cumulative source remains a prior custody transition; only its
    rebuildable ``fills`` contribution is replaced. ``exact_execution`` is
    the incoming trigger; ``prior_exact_observations`` retains every earlier
    quarantined exact and its original custody clocks. The fact records both
    proof aggregates and the authority revision that admitted the replacement
    so replay can independently reject a stale or broadened plan.
    """

    actor: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    expected_control_revision: int
    order_ref: str
    symbol: str
    side: str
    superseded_cumulative_fill_ids: list[str]
    exact_execution: ExecutionSliceFilledFacts
    exact_quantity: float
    exact_gross_cost: float
    cumulative_quantity: float
    cumulative_gross_cost: float
    quantity_tolerance: float
    gross_cost_tolerance: float
    resolved_uncertainty_id: str | None
    evidence_refs: list[str]
    prior_exact_observations: list[ExecutionCoverageExactProvenance] = field(
        default_factory=list
    )

    def to_facts_json(self) -> str:
        value = asdict(self)
        value["exact_execution"] = json.loads(self.exact_execution.to_facts_json())
        if not self.prior_exact_observations:
            # Keep #1554's direct-transition representation byte-compatible.
            value.pop("prior_exact_observations")
        return canonicalize(value)

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExecutionCoverageSupersededFacts:
        value = json.loads(facts_json)
        if not isinstance(value, dict):
            raise ValueError("coverage supersession facts must be an object")
        try:
            value["exact_execution"] = ExecutionSliceFilledFacts.from_facts_json(
                canonicalize(value["exact_execution"])
            )
            value["prior_exact_observations"] = [
                _exact_provenance_from_mapping(item)
                for item in value.pop("prior_exact_observations", [])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("coverage supersession exact execution is invalid") from exc
        return cls(**value)


def _exact_provenance_from_mapping(item: dict) -> ExecutionCoverageExactProvenance:
    """One recorded exact provenance, read back from its facts JSON object."""
    return ExecutionCoverageExactProvenance(
        exact_execution=ExecutionSliceFilledFacts.from_facts_json(canonicalize(item["exact_execution"])),
        observation_transition_sequence=item["observation_transition_sequence"],
        clerk_observed_at_ms=item["clerk_observed_at_ms"],
        recorded_at_ms=item["recorded_at_ms"],
        recorded_transition_sequence=item["recorded_transition_sequence"],
    )


#: The transition kind and summary code of the chain-total coverage proof (#2786).
CHAIN_TOTAL_PROVEN_TRANSITION = "EXECUTION_COVERAGE_CHAIN_TOTAL_PROVEN"


@dataclass(frozen=True)
class ExecutionCoverageChainTotalProvenFacts:
    """A filled manual chain's exact executions replacing its cumulative coverage (#2786).

    ``head_quantity`` is the chain head's own requested quantity, read from
    the head's observation that proved the total: no other durable row holds
    it. Every other figure is re-read from current rows by the fold, which
    refuses a plan they no longer match. ``made_effective_exact_observations``
    are the quarantined exacts that become effective fills, each with its
    original custody clocks; ``effective_exact_execution_ids`` were effective
    already. ``position_delta`` is ``exact_quantity - prior_effective_quantity``,
    the one coverage change that moves the position.
    """

    actor: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    expected_control_revision: int
    order_ref: str
    symbol: str
    side: str
    head_broker_order_id: str
    head_quantity: float
    superseded_cumulative_fill_ids: list[str]
    effective_exact_execution_ids: list[str]
    made_effective_exact_observations: list[ExecutionCoverageExactProvenance]
    exact_quantity: float
    prior_effective_quantity: float
    position_delta: float
    quantity_tolerance: float
    resolved_uncertainty_id: str | None
    evidence_refs: list[str]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExecutionCoverageChainTotalProvenFacts:
        value = json.loads(facts_json)
        if not isinstance(value, dict):
            raise ValueError("chain-total coverage facts must be an object")
        try:
            value["made_effective_exact_observations"] = [
                _exact_provenance_from_mapping(item) for item in value["made_effective_exact_observations"]
            ]
            return cls(**value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("chain-total coverage facts are invalid") from exc


@dataclass(frozen=True)
class ExecutionCoverageSetCandidate:
    """All immutable observations required for one automatic set-proof attempt.

    ``order_effective_quantity`` and ``order_effective_gross_cost`` are the
    order's effective fills before the swap -- its effective exacts and the
    cumulative-recovery rows -- whose average the swap may move by less than
    one price increment.
    """

    cumulative_recovery: tuple[CumulativeCoverageObservation, ...]
    prior_quarantined_exact: tuple[ExactCoverageObservation, ...]
    incoming_exact: ExactCoverageObservation
    order_effective_quantity: float
    order_effective_gross_cost: float
    active_episode_ids: tuple[str, ...] = ()
    effective_exact_source_ids: frozenset[str] = field(default_factory=frozenset)
    unreadable_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionCoverageAggregate:
    """Deterministic aggregate economics, in shares and currency units."""

    quantity: float
    gross_cost: float
    vwap: float


class ExecutionCoverageSetProofRefusalReason(StrEnum):
    """Every fail-closed outcome the pure set proof can explain."""

    MISSING_CUMULATIVE_COVERAGE = "missing_cumulative_coverage"
    UNREADABLE_EVIDENCE = "unreadable_evidence"
    MULTIPLE_ACTIVE_EPISODES = "multiple_active_episodes"
    INVALID_SOURCE_IDENTITY = "invalid_source_identity"
    DUPLICATE_SOURCE_ID = "duplicate_source_id"
    EXACT_ALREADY_EFFECTIVE = "exact_already_effective"
    IDENTITY_MISMATCH = "identity_mismatch"
    NONFINITE_QUANTITY = "nonfinite_quantity"
    NONPOSITIVE_QUANTITY = "nonpositive_quantity"
    NONFINITE_PRICE = "nonfinite_price"
    NONFINITE_FEE = "nonfinite_fee"
    NONFINITE_GROSS_COST = "nonfinite_gross_cost"
    NONFINITE_AGGREGATE = "nonfinite_aggregate"
    INVALID_ORDER_TOTAL = "invalid_order_total"
    QUANTITY_MISMATCH = "quantity_mismatch"
    VWAP_MISMATCH = "vwap_mismatch"


@dataclass(frozen=True)
class ExecutionCoverageSetProofSuccess:
    """A fully explained no-position-delta replacement proof."""

    exact: ExecutionCoverageAggregate
    cumulative: ExecutionCoverageAggregate
    position_delta: float
    gross_cost_tolerance: float
    retained_exact_observations: tuple[ExactCoverageObservation, ...]


@dataclass(frozen=True)
class ExecutionCoverageSetProofRefusal:
    """A specific malformed-state or economic reason to retain quarantine."""

    reason: ExecutionCoverageSetProofRefusalReason
    detail: str


ExecutionCoverageSetProofResult = ExecutionCoverageSetProofSuccess | ExecutionCoverageSetProofRefusal


def prove_execution_coverage_set(
    candidate: ExecutionCoverageSetCandidate,
) -> ExecutionCoverageSetProofResult:
    """Prove whether complete cumulative and exact sets carry equal economics.

    Quantities must agree strictly. Prices are compared at Alpaca's price
    precision, never at float precision (#2791): the cumulative's price comes
    from the broker's rounded order average, so exact executions explain it
    when they move the order's average by less than one valid increment
    (:func:`execution_coverage_gross_cost_tolerance`).

    Fees are deliberately absent from the arithmetic because cumulative recovery
    has no fee observation. The success result returns the exact observations
    unchanged, preserving any reported exact fees for the caller's fold.
    """
    if not candidate.cumulative_recovery:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.MISSING_CUMULATIVE_COVERAGE,
            "No cumulative-recovery rows are available for the proposed replacement.",
        )
    if candidate.unreadable_source_ids:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.UNREADABLE_EVIDENCE,
            "Immutable coverage evidence is unreadable: "
            + ", ".join(sorted(candidate.unreadable_source_ids)),
        )
    if len(candidate.active_episode_ids) > 1:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.MULTIPLE_ACTIVE_EPISODES,
            "More than one active execution-coverage uncertainty episode is incompatible with automatic resolution.",
        )

    exact = (*candidate.prior_quarantined_exact, candidate.incoming_exact)
    observations = candidate.cumulative_recovery + exact
    source_ids = tuple(observation.source_id for observation in observations)
    if any(not isinstance(source_id, str) or not source_id for source_id in source_ids):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.INVALID_SOURCE_IDENTITY,
            "Every cumulative and exact observation requires a non-empty source identity.",
        )
    if len(source_ids) != len(set(source_ids)):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.DUPLICATE_SOURCE_ID,
            "Coverage proof observations contain a duplicate immutable source identity.",
        )
    if any(observation.source_id in candidate.effective_exact_source_ids for observation in exact):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.EXACT_ALREADY_EFFECTIVE,
            "A candidate exact execution is already effective and cannot be inserted again.",
        )

    identity = candidate.incoming_exact.identity
    if any(observation.identity != identity for observation in observations):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.IDENTITY_MISMATCH,
            "Coverage observations must share account, authority, order, symbol, and side identity.",
        )
    validation = _validate_set_economics(observations)
    if validation is not None:
        return validation

    try:
        exact_aggregate = _aggregate_coverage(exact)
        cumulative_aggregate = _aggregate_coverage(candidate.cumulative_recovery)
    except OverflowError:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.NONFINITE_AGGREGATE,
            "Coverage rows cannot be accumulated into finite aggregate economics.",
        )
    if not _is_finite(exact_aggregate.gross_cost) or not _is_finite(cumulative_aggregate.gross_cost):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.NONFINITE_GROSS_COST,
            "Coverage quantity and price multiply to a non-finite gross cost.",
        )
    position_delta = exact_aggregate.quantity - cumulative_aggregate.quantity
    if abs(position_delta) >= QTY_ATOL:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.QUANTITY_MISMATCH,
            "Exact and cumulative coverage quantities differ outside the strict share tolerance.",
        )
    order_quantity = candidate.order_effective_quantity
    order_gross_cost = candidate.order_effective_gross_cost
    if (
        not _is_finite(order_quantity)
        or not _is_finite(order_gross_cost)
        or order_quantity <= 0
        or order_gross_cost < 0
        or cumulative_aggregate.quantity - order_quantity >= QTY_ATOL
    ):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.INVALID_ORDER_TOTAL,
            "The order's effective fills are not a positive total that holds its cumulative recovery.",
        )
    cost_tolerance = execution_coverage_gross_cost_tolerance(
        exact=exact_aggregate,
        cumulative=cumulative_aggregate,
        order_quantity=order_quantity,
        order_gross_cost=order_gross_cost,
    )
    if not abs(exact_aggregate.gross_cost - cumulative_aggregate.gross_cost) < cost_tolerance:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.VWAP_MISMATCH,
            "The exact executions would move the order's average price by one valid increment or more.",
        )
    return ExecutionCoverageSetProofSuccess(
        exact=exact_aggregate,
        cumulative=cumulative_aggregate,
        position_delta=position_delta,
        gross_cost_tolerance=cost_tolerance,
        retained_exact_observations=tuple(sorted(exact, key=lambda observation: observation.source_id)),
    )


#: Broker order states whose reported cumulative filled quantity is final: no
#: later execution can join the order, so the broker's total bounds every
#: exact slice it will ever report (#2346). These are exactly the canonical
#: terminal statuses. ``replaced`` is final for its own order ID: Alpaca books
#: every later execution on the replacement order, which carries its own
#: ``order_ref``. ``rejected`` reports no fill, so its absent total can never
#: prove a quarantined exact, and a rejection that did report one is still
#: that order's last word.
FINAL_CUMULATIVE_BROKER_STATES = ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES

#: The ``UNCERTAINTY_RESOLVED`` vocabulary of the order-level coverage proof.
ORDER_TOTAL_PROVEN_RESOLUTION_KIND = "ORDER_TOTAL_PROVEN"
ORDER_TOTAL_PROVEN_SUMMARY_CODE = "EXECUTION_COVERAGE_ORDER_TOTAL_PROVEN"


@dataclass(frozen=True)
class OrderTotalCoverageEvidence:
    """Recorded broker and Clerk totals for one conflicted order, in shares."""

    broker_state: str | None
    reported_filled_quantity: float | None
    effective_quantity: float
    cumulative_recovery_quantity: float
    quarantined_exact_quantities: tuple[float, ...]


def order_total_proves_coverage(evidence: OrderTotalCoverageEvidence) -> bool:
    """Whether the broker's final order total already accounts for every quarantined exact.

    The set proof above needs every exact slice of a cumulative row. A slice
    Alpaca never re-sends (a websocket outage, a dropped frame) leaves that
    set incomplete for ever, so this is the order-level proof: once the
    broker's cumulative is final and the effective fills equal it, every
    execution of the order is already in the position, and the quarantined
    exacts (distinct broker identities) are the part the cumulative-recovery
    rows stand for. Resolving the episode changes no fill. A broker total
    that disagrees with the recorded fills, or quarantined exacts that exceed
    the cumulative recovery, keep the episode open.

    Formula: state ∈ FINAL_CUMULATIVE_BROKER_STATES
      ∧ |Q_effective − Q_broker| < QTY_ATOL
      ∧ fsum(q_quarantined) − Q_cumulative_recovery < QTY_ATOL (shares).
    Reference: Project-authored order-level coverage proof for issue #2346,
      extending the PRD #1543 execution-coverage contract.
    Canonical implementation: this function.
    Validated against: tests/broker/alpaca/clerk/sqlite/
      test_execution_coverage_order_total_proof.py.
    """
    if (evidence.broker_state or "").lower() not in FINAL_CUMULATIVE_BROKER_STATES:
        return False
    reported = evidence.reported_filled_quantity
    quarantined = evidence.quarantined_exact_quantities
    if reported is None or not quarantined:
        return False
    values = (reported, evidence.effective_quantity, evidence.cumulative_recovery_quantity, *quarantined)
    if not all(_is_finite(value) for value in values) or any(quantity <= 0 for quantity in quarantined):
        return False
    return (
        abs(evidence.effective_quantity - reported) < QTY_ATOL
        and math.fsum(quarantined) - evidence.cumulative_recovery_quantity < QTY_ATOL
    )


@dataclass(frozen=True)
class ChainTotalCoverageEvidence:
    """A manual leg's chain head and every distinct exact execution of its chain, in shares."""

    head_state: str | None
    head_quantity: float | None
    exact_quantities: tuple[float, ...]


def chain_total_proves_coverage(evidence: ChainTotalCoverageEvidence) -> bool:
    """Whether a filled manual chain's exact executions are every share its head asked for (#2786).

    The order-total proof above needs the broker's cumulative to equal the
    recorded fills. A manual leg Alpaca replaced has no such figure: a
    replacement's ``filled_qty`` may leave out its original's fills
    (:mod:`manual_order_replacement`), so a cumulative folded from it can
    under-credit the chain, and the exacts that arrive later conflict with
    it for ever. The chain has a stronger final figure instead: a head that
    reports ``filled`` executed its own ``qty``, which is the whole chain's
    total. When the chain's distinct exact executions sum to it, they are
    every execution of the chain, so they replace every cumulative-recovery
    row and the leg's position becomes their total -- the one coverage proof
    that moves the position. An unreplaced manual leg is a chain of one.

    Formula: state = filled ∧ Q_head > 0 ∧ every q_exact finite and > 0
      ∧ |fsum(q_exact) − Q_head| < QTY_ATOL (shares, rtol = 0).
    Reference: Project-authored chain-total coverage proof for issue #2786,
      extending the #2346 order-level proof to a manual replacement chain;
      ADR 0036, 2026-10-01 amendment.
    Canonical implementation: this function.
    Validated against: tests/broker/alpaca/clerk/sqlite/
      test_manual_order_chain_total_proof.py.
    """
    if (evidence.head_state or "").lower() != "filled":
        return False
    head_quantity = evidence.head_quantity
    exact = evidence.exact_quantities
    if head_quantity is None or not exact:
        return False
    if not all(_is_finite(value) for value in (head_quantity, *exact)):
        return False
    if head_quantity <= 0 or any(quantity <= 0 for quantity in exact):
        return False
    return abs(math.fsum(exact) - head_quantity) < QTY_ATOL


def order_total_retained_exacts_explain_cumulative(
    *,
    retained_quantities: tuple[float, ...],
    cumulative_quantities: tuple[float, ...],
) -> bool:
    """Whether the exacts an order-total proof kept quarantined are every share of the order's cumulative (#2791).

    The order-level proof above closes an episode without moving a fill, so
    an order whose exacts disagree with its broker total by a price
    increment or more keeps its cumulative-recovery rows for ever, and no
    fill names the executions behind them. When the retained exacts hold
    every share those rows hold, each of those executions is still named:
    the effective exacts and the retained ones together are the broker's
    final total. A fee population needs only that: which executions there
    were, not whose price stands. Quantities only, as in the proof itself.

    Formula: every q finite and > 0
      ∧ |fsum(q_retained) − fsum(q_cumulative)| < QTY_ATOL (shares, rtol = 0).
    Reference: Project-authored for issue #2791, on the #2346 order-level
      proof (:func:`order_total_proves_coverage`).
    Canonical implementation: this function.
    Validated against: tests/broker/alpaca/clerk/sqlite/
      test_bot_order_activity_disagreement.py.
    """
    if not retained_quantities or not cumulative_quantities:
        return False
    quantities = (*retained_quantities, *cumulative_quantities)
    if not all(_is_finite(quantity) and quantity > 0 for quantity in quantities):
        return False
    return abs(math.fsum(retained_quantities) - math.fsum(cumulative_quantities)) < QTY_ATOL


def _validate_set_economics(
    observations: tuple[CumulativeCoverageObservation | ExactCoverageObservation, ...],
) -> ExecutionCoverageSetProofRefusal | None:
    for observation in observations:
        if not _is_finite(observation.quantity):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_QUANTITY,
                f"Coverage quantity is non-finite for source {observation.source_id!r}.",
            )
        if observation.quantity <= 0:
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONPOSITIVE_QUANTITY,
                f"Coverage quantity must be positive for source {observation.source_id!r}.",
            )
        if not _is_finite(observation.price):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_PRICE,
                f"Coverage price is non-finite for source {observation.source_id!r}.",
            )
        if not _is_finite(observation.quantity * observation.price):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_GROSS_COST,
                f"Coverage gross cost is non-finite for source {observation.source_id!r}.",
            )
        if isinstance(observation, ExactCoverageObservation) and observation.fee is not None and not _is_finite(observation.fee):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_FEE,
                f"Exact execution fee is non-finite for source {observation.source_id!r}.",
            )
    return None


def _aggregate_coverage(
    observations: tuple[CumulativeCoverageObservation | ExactCoverageObservation, ...],
) -> ExecutionCoverageAggregate:
    ordered = tuple(sorted(observations, key=lambda observation: observation.source_id))
    quantity = math.fsum(observation.quantity for observation in ordered)
    gross_cost = math.fsum(observation.quantity * observation.price for observation in ordered)
    return ExecutionCoverageAggregate(
        quantity=quantity,
        gross_cost=gross_cost,
        vwap=gross_cost / quantity,
    )


def execution_coverage_gross_cost_tolerance(
    *,
    exact: ExecutionCoverageAggregate,
    cumulative: ExecutionCoverageAggregate,
    order_quantity: float,
    order_gross_cost: float,
) -> float:
    """The gross-cost gap at which exact executions move the order's average by one price increment.

    ``tick(C_O / Q_O) × Q_O``: the cumulative carries the rounding of the
    broker's order average over every share of the order, however few of them
    the cumulative rows hold, so the gap is measured on the order's whole
    average rather than on the rows'. ``max(|P_E|, |P_R|) × |Q_E − Q_R|``
    prices the share residue, under ``QTY_ATOL``, the quantity rule accepted;
    with equal quantities it is zero, so a gap of one increment is refused.
    """
    return total_price_conflict_atol(order_gross_cost / order_quantity) * order_quantity + max(
        abs(exact.vwap), abs(cumulative.vwap)
    ) * abs(exact.quantity - cumulative.quantity)


def strict_gross_cost_envelope(
    *,
    exact: ExecutionCoverageAggregate,
    cumulative: ExecutionCoverageAggregate,
) -> float:
    """The float-precision envelope coverage was proven with before #2791, kept so those records replay."""
    return (
        max(abs(exact.quantity), abs(cumulative.quantity)) * PRICE_ATOL
        + max(abs(exact.vwap), abs(cumulative.vwap)) * QTY_ATOL
        + QTY_ATOL * PRICE_ATOL
    )


def _is_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _set_refusal(
    reason: ExecutionCoverageSetProofRefusalReason,
    detail: str,
) -> ExecutionCoverageSetProofRefusal:
    return ExecutionCoverageSetProofRefusal(reason=reason, detail=detail)


def validate_execution_coverage_superseded_facts(
    facts: ExecutionCoverageSupersededFacts,
) -> ExecutionSliceFilledFacts:
    """Validate the closed direct-coverage replacement record.

    The fold re-runs the canonical proof against current immutable rows. This
    boundary validator instead makes the recorded plan self-contained and
    unambiguous before it reaches the custody hash chain.
    """
    if facts.actor != "AUTOMATIC":
        raise ValueError("coverage supersession actor must be AUTOMATIC")
    if not isinstance(facts.account_id, str) or not facts.account_id:
        raise ValueError("coverage supersession requires account_id")
    if (
        isinstance(facts.authority_generation, bool)
        or not isinstance(facts.authority_generation, int)
        or facts.authority_generation < 1
    ):
        raise ValueError("coverage supersession requires authority_generation")
    if not isinstance(facts.db_identity_token, str) or not facts.db_identity_token:
        raise ValueError("coverage supersession requires db_identity_token")
    if (
        isinstance(facts.expected_control_revision, bool)
        or not isinstance(facts.expected_control_revision, int)
        or facts.expected_control_revision < 0
    ):
        raise ValueError("coverage supersession requires expected_control_revision")
    if not isinstance(facts.order_ref, str) or not facts.order_ref:
        raise ValueError("coverage supersession requires order_ref")
    if not isinstance(facts.symbol, str) or not facts.symbol:
        raise ValueError("coverage supersession requires symbol")
    if facts.side not in {"BUY", "SELL"}:
        raise ValueError("coverage supersession requires an exact side")
    fill_ids = facts.superseded_cumulative_fill_ids
    if (
        not isinstance(fill_ids, list)
        or not fill_ids
        or any(not isinstance(fill_id, str) or not fill_id for fill_id in fill_ids)
        or fill_ids != sorted(set(fill_ids))
    ):
        raise ValueError("coverage supersession requires sorted cumulative fill ids")
    exact = facts.exact_execution
    validate_execution_slice_facts(exact)
    if exact.symbol.upper() != facts.symbol.upper() or exact.side != facts.side:
        raise ValueError("coverage supersession exact identity does not match its authority")
    _require_finite_positive_coverage(facts.exact_quantity, field="exact_quantity")
    _require_finite_positive_coverage(facts.exact_gross_cost, field="exact_gross_cost")
    _require_finite_positive_coverage(facts.cumulative_quantity, field="cumulative_quantity")
    _require_finite_positive_coverage(facts.cumulative_gross_cost, field="cumulative_gross_cost")
    _require_finite_positive_coverage(facts.quantity_tolerance, field="quantity_tolerance")
    _require_finite_nonnegative_coverage(facts.gross_cost_tolerance, field="gross_cost_tolerance")
    if facts.resolved_uncertainty_id is not None and (
        not isinstance(facts.resolved_uncertainty_id, str) or not facts.resolved_uncertainty_id
    ):
        raise ValueError("coverage supersession resolved uncertainty id is invalid")
    prior = facts.prior_exact_observations
    if not isinstance(prior, list):
        raise ValueError("coverage supersession prior exact observations must be a list")
    prior_ids = [item.exact_execution.execution_id for item in prior]
    if prior_ids != sorted(set(prior_ids)) or exact.execution_id in prior_ids:
        raise ValueError("coverage supersession prior exact identities must be sorted and unique")
    for item in prior:
        _validate_exact_provenance(item, symbol=facts.symbol, side=facts.side, subject="coverage supersession prior")
    all_exact = [*(item.exact_execution for item in prior), exact]
    expected_exact_quantity = math.fsum(item.slice_qty for item in all_exact)
    expected_exact_gross_cost = math.fsum(
        item.slice_qty * item.slice_price for item in all_exact
    )
    if (
        facts.exact_quantity != expected_exact_quantity
        or facts.exact_gross_cost != expected_exact_gross_cost
    ):
        raise ValueError("coverage supersession exact aggregate is not its exact economics")
    if (
        not isinstance(facts.evidence_refs, list)
        or not facts.evidence_refs
        or any(not isinstance(reference, str) or not reference for reference in facts.evidence_refs)
        or facts.evidence_refs != sorted(set(facts.evidence_refs))
    ):
        raise ValueError("coverage supersession requires unique sorted evidence references")
    return exact


def _require_finite_positive_coverage(value: object, *, field: str) -> None:
    if not _is_finite(value) or value <= 0:
        raise ValueError(f"{field} must be finite and positive")


def _require_finite_nonnegative_coverage(value: object, *, field: str) -> None:
    if not _is_finite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")


def _validate_exact_provenance(
    item: ExecutionCoverageExactProvenance, *, symbol: str, side: str, subject: str
) -> None:
    """One retained exact's facts and custody clocks, for the leg ``symbol``/``side``."""
    validate_execution_slice_facts(item.exact_execution)
    if (
        item.exact_execution.symbol.upper() != symbol.upper()
        or item.exact_execution.side != side
        or item.observation_transition_sequence != item.recorded_transition_sequence
    ):
        raise ValueError(f"{subject} exact provenance is inconsistent")
    for field_name in (
        "observation_transition_sequence",
        "recorded_transition_sequence",
        "clerk_observed_at_ms",
        "recorded_at_ms",
    ):
        value = getattr(item, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{subject} {field_name} is invalid")


def _require_sorted_unique_strings(values: object, *, field: str) -> None:
    if (
        not isinstance(values, list)
        or any(not isinstance(value, str) or not value for value in values)
        or values != sorted(set(values))
    ):
        raise ValueError(f"{field} must be sorted, unique, non-empty strings")


def validate_execution_coverage_chain_total_proven_facts(
    facts: ExecutionCoverageChainTotalProvenFacts,
) -> None:
    """Validate the closed chain-total coverage record before it reaches the hash chain (#2786).

    The fold re-reads every figure but ``head_quantity`` from current rows;
    this boundary makes the recorded plan self-contained: its binding, its
    exact set, and the arithmetic that ties its totals to its position delta.
    """
    if facts.actor != "AUTOMATIC":
        raise ValueError("chain-total coverage actor must be AUTOMATIC")
    for field_name in ("account_id", "db_identity_token", "order_ref", "symbol", "head_broker_order_id"):
        value = getattr(facts, field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"chain-total coverage requires {field_name}")
    if (
        isinstance(facts.authority_generation, bool)
        or not isinstance(facts.authority_generation, int)
        or facts.authority_generation < 1
    ):
        raise ValueError("chain-total coverage requires authority_generation")
    if (
        isinstance(facts.expected_control_revision, bool)
        or not isinstance(facts.expected_control_revision, int)
        or facts.expected_control_revision < 0
    ):
        raise ValueError("chain-total coverage requires expected_control_revision")
    if facts.side not in {"BUY", "SELL"}:
        raise ValueError("chain-total coverage requires an exact side")
    _require_finite_positive_coverage(facts.head_quantity, field="head_quantity")
    _require_finite_positive_coverage(facts.exact_quantity, field="exact_quantity")
    _require_finite_nonnegative_coverage(facts.prior_effective_quantity, field="prior_effective_quantity")
    if facts.quantity_tolerance != QTY_ATOL:
        raise ValueError("chain-total coverage quantity tolerance is not the pinned QTY_ATOL")
    if not _is_finite(facts.position_delta) or (
        facts.position_delta != facts.exact_quantity - facts.prior_effective_quantity
    ):
        raise ValueError("chain-total coverage position delta is not its exact total less its prior fills")
    if abs(facts.exact_quantity - facts.head_quantity) >= facts.quantity_tolerance:
        raise ValueError("chain-total coverage exact total does not cover the head's quantity")
    _require_sorted_unique_strings(facts.superseded_cumulative_fill_ids, field="superseded_cumulative_fill_ids")
    _require_sorted_unique_strings(facts.effective_exact_execution_ids, field="effective_exact_execution_ids")
    made_effective = facts.made_effective_exact_observations
    if not isinstance(made_effective, list):
        raise ValueError("chain-total coverage made-effective exacts must be a list")
    made_effective_ids = [item.exact_execution.execution_id for item in made_effective]
    if made_effective_ids != sorted(set(made_effective_ids)) or set(made_effective_ids) & set(
        facts.effective_exact_execution_ids
    ):
        raise ValueError("chain-total coverage exact identities must be sorted and distinct")
    for item in made_effective:
        _validate_exact_provenance(item, symbol=facts.symbol, side=facts.side, subject="chain-total coverage")
    if not facts.superseded_cumulative_fill_ids and not made_effective:
        raise ValueError("chain-total coverage must supersede a cumulative fill or make an exact effective")
    if facts.resolved_uncertainty_id is not None and (
        not isinstance(facts.resolved_uncertainty_id, str) or not facts.resolved_uncertainty_id
    ):
        raise ValueError("chain-total coverage resolved uncertainty id is invalid")
    _require_sorted_unique_strings(facts.evidence_refs, field="evidence_refs")
    if not facts.evidence_refs:
        raise ValueError("chain-total coverage requires evidence references")


@dataclass(frozen=True)
class ActiveExecutionCoverageConflict:
    """One active coverage-conflict episode, rooted at its first exact ID."""

    uncertainty_id: str
    strategy_instance_id: str | None
    order_ref: str
    conflict_execution_id: str


@dataclass(frozen=True)
class CumulativeRecoveryFill:
    """The aggregate fill which a closed proof may replace once."""

    fill_id: str
    order_ref: str
    quantity: float
    price: float
    side: str


@dataclass(frozen=True)
class ExecutionCoverageProof:
    """The full immutable evidence set and its deliberately narrow result."""

    conflict: ActiveExecutionCoverageConflict
    quarantined: tuple[ExecutionCoverageQuarantinedFacts, ...]
    cumulative: CumulativeRecoveryFill | None
    proof_available: bool
    unavailable_reason: str | None

    @property
    def execution_ids(self) -> tuple[str, ...]:
        return tuple(item.exact_execution.execution_id for item in self.quarantined)

    @property
    def exact_execution(self) -> ExecutionSliceFilledFacts | None:
        if len(self.quarantined) != 1:
            return None
        return self.quarantined[0].exact_execution


def active_execution_coverage_conflicts(
    conn: sqlite3.Connection,
    *,
    order_ref: str | None = None,
    uncertainty_id: str | None = None,
) -> tuple[ActiveExecutionCoverageConflict, ...]:
    """Read well-formed active episodes without relying on SQLite JSON support."""
    rows = conn.execute(
        "SELECT uncertainty_id, strategy_instance_id, facts_json FROM uncertainties "
        "WHERE reason_code = ? AND resolved_at_ms IS NULL ORDER BY observed_at_ms ASC, uncertainty_id ASC",
        (EXECUTION_COVERAGE_CONFLICT_REASON_CODE,),
    ).fetchall()
    conflicts: list[ActiveExecutionCoverageConflict] = []
    for row in rows:
        if uncertainty_id is not None and row["uncertainty_id"] != uncertainty_id:
            continue
        try:
            raised = UncertaintyRaisedFacts.from_facts_json(row["facts_json"])
            cause = ExecutionCoverageConflictCause.from_mapping(raised.cause_facts)
        except (KeyError, TypeError, ValueError):
            continue
        if order_ref is not None and cause.order_ref != order_ref:
            continue
        conflicts.append(
            ActiveExecutionCoverageConflict(
                uncertainty_id=row["uncertainty_id"],
                strategy_instance_id=row["strategy_instance_id"],
                order_ref=cause.order_ref,
                conflict_execution_id=cause.execution_id,
            )
        )
    return tuple(conflicts)


def quarantined_executions_for_conflict(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> tuple[ExecutionCoverageQuarantinedFacts, ...]:
    """Return every distinct exact slice retained for one active episode."""
    return tuple(
        observation.facts
        for observation in first_quarantine_per_execution(
            quarantine_observations_for_order(conn, order_ref=conflict.order_ref),
            conflict_execution_ids=frozenset({conflict.conflict_execution_id}),
        )
        if observation.facts is not None
    )


@dataclass(frozen=True)
class QuarantineObservation:
    """One ``EXECUTION_COVERAGE_QUARANTINED`` transition; ``facts`` is ``None`` when unreadable."""

    sequence: int
    clerk_observed_at_ms: int
    recorded_at_ms: int
    facts: ExecutionCoverageQuarantinedFacts | None


def quarantine_observations_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> tuple[QuarantineObservation, ...]:
    """The one reader of an order's quarantine transitions, readable or not, in sequence order.

    Callers that prove anything from these rows refuse on any unreadable one
    rather than proving around it.
    """
    rows = conn.execute(
        "SELECT sequence, clerk_observed_at_ms, recorded_at_ms, facts_json "
        "FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'EXECUTION_COVERAGE_QUARANTINED' ORDER BY sequence ASC",
        (order_ref,),
    ).fetchall()
    observations: list[QuarantineObservation] = []
    for row in rows:
        facts: ExecutionCoverageQuarantinedFacts | None
        try:
            facts = ExecutionCoverageQuarantinedFacts.from_facts_json(row["facts_json"])
            validate_execution_coverage_quarantined_facts(facts)
        except (KeyError, TypeError, ValueError):
            facts = None
        observations.append(
            QuarantineObservation(
                sequence=row["sequence"],
                clerk_observed_at_ms=row["clerk_observed_at_ms"],
                recorded_at_ms=row["recorded_at_ms"],
                facts=facts,
            )
        )
    return tuple(observations)


def first_quarantine_per_execution(
    observations: tuple[QuarantineObservation, ...],
    *,
    conflict_execution_ids: frozenset[str],
) -> tuple[QuarantineObservation, ...]:
    """The first readable quarantine of each exact slice the named episodes retain."""
    retained: list[QuarantineObservation] = []
    seen_execution_ids: set[str] = set()
    for observation in observations:
        facts = observation.facts
        if facts is None or facts.conflict_execution_id not in conflict_execution_ids:
            continue
        execution_id = facts.exact_execution.execution_id
        if execution_id in seen_execution_ids:
            continue
        seen_execution_ids.add(execution_id)
        retained.append(observation)
    return tuple(retained)


def order_total_proven_conflict_execution_ids(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> frozenset[str]:
    """The root exact IDs of the order's episodes an order-total proof closed (#2346).

    Their quarantined exacts are accounted for by the broker's final total but
    are not effective fills, so a later exact may still complete their set
    proof. An episode whose raised facts are unreadable contributes nothing,
    which leaves any later set proof short and therefore fail-closed.
    """
    resolutions = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'UNCERTAINTY_RESOLVED' AND summary_code = ?",
        (order_ref, ORDER_TOTAL_PROVEN_SUMMARY_CODE),
    ).fetchall()
    execution_ids: set[str] = set()
    for resolution in resolutions:
        resolved = UncertaintyResolvedFacts.from_facts_json(resolution["facts_json"])
        if resolved.resolution_kind != ORDER_TOTAL_PROVEN_RESOLUTION_KIND:
            continue
        raised = conn.execute(
            "SELECT facts_json FROM uncertainties WHERE uncertainty_id = ? AND reason_code = ?",
            (resolved.uncertainty_id, EXECUTION_COVERAGE_CONFLICT_REASON_CODE),
        ).fetchone()
        if raised is None:
            continue
        try:
            cause = ExecutionCoverageConflictCause.from_mapping(
                UncertaintyRaisedFacts.from_facts_json(raised["facts_json"]).cause_facts
            )
        except (KeyError, TypeError, ValueError):
            continue
        if cause.order_ref == order_ref:
            execution_ids.add(cause.execution_id)
    return frozenset(execution_ids)


def execution_is_quarantined(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
    execution_id: str,
) -> bool:
    """Whether the immutable broker execution identity was already retained."""
    return any(
        item.exact_execution.execution_id == execution_id
        for item in quarantined_executions_for_conflict(conn, conflict=conflict)
    )


def _has_unreadable_quarantine_for_order(conn: sqlite3.Connection, *, order_ref: str) -> bool:
    """Fail closed rather than proving around malformed retained evidence."""
    from app.broker.alpaca.clerk.sqlite.execution_coverage_evidence import (
        unreadable_quarantine_source_ids_for_order,
    )

    return bool(unreadable_quarantine_source_ids_for_order(conn, order_ref=order_ref))


def execution_coverage_proof(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> ExecutionCoverageProof:
    """Prove only the S0 one-exact-for-one-cumulative replacement case."""
    quarantined = quarantined_executions_for_conflict(conn, conflict=conflict)
    if _has_unreadable_quarantine_for_order(conn, order_ref=conflict.order_ref):
        return _unavailable(
            conflict,
            quarantined,
            "Execution coverage evidence is unreadable; no safe replacement can be proven.",
        )
    if not quarantined:
        return _unavailable(conflict, quarantined, "The exact execution quarantine is absent; fresh exact evidence is required.")
    if len(quarantined) != 1:
        return _unavailable(
            conflict,
            quarantined,
            "Multiple exact executions are quarantined for this aggregate recovery total; no one-slice replacement is safe.",
            cumulative=_current_cumulative_recovery_fill(conn, quarantined[0]),
        )
    quarantine = quarantined[0]
    exact = quarantine.exact_execution
    if exact.execution_id != conflict.conflict_execution_id:
        return _unavailable(
            conflict,
            quarantined,
            "The coverage episode is missing its originating exact execution; no safe replacement can be proven.",
        )
    if len(quarantine.conflicting_cumulative_fill_ids) != 1:
        return _unavailable(
            conflict,
            quarantined,
            "The exact execution overlaps multiple cumulative recovery rows; fresh per-slice evidence is required.",
        )
    cumulative = _current_cumulative_recovery_fill(conn, quarantine)
    if cumulative is None:
        return _unavailable(
            conflict,
            quarantined,
            "The cumulative recovery row is unavailable; fresh exact evidence is required.",
        )
    if cumulative.order_ref != conflict.order_ref:
        return _unavailable(
            conflict,
            quarantined,
            "The cumulative recovery row belongs to a different order; no safe replacement can be proven.",
            cumulative=cumulative,
        )
    if not exact_replaces_cumulative(exact=exact, cumulative=cumulative):
        return ExecutionCoverageProof(
            conflict=conflict,
            quarantined=quarantined,
            cumulative=cumulative,
            proof_available=False,
            unavailable_reason="The exact execution and cumulative recovery economics differ; no automatic replacement is safe.",
        )
    return ExecutionCoverageProof(
        conflict=conflict,
        quarantined=quarantined,
        cumulative=cumulative,
        proof_available=True,
        unavailable_reason=None,
    )


def _current_cumulative_recovery_fill(
    conn: sqlite3.Connection,
    quarantine: ExecutionCoverageQuarantinedFacts,
) -> CumulativeRecoveryFill | None:
    """Read the single aggregate identity carried by one quarantined slice."""
    if len(quarantine.conflicting_cumulative_fill_ids) != 1:
        return None
    return cumulative_recovery_fill_by_id(
        conn,
        fill_id=quarantine.conflicting_cumulative_fill_ids[0],
    )


def cumulative_recovery_fill_by_id(
    conn: sqlite3.Connection,
    *,
    fill_id: str,
) -> CumulativeRecoveryFill | None:
    """Return one typed aggregate-recovery row without inferring any exact slice."""
    row = conn.execute(
        "SELECT fill_id, order_ref, qty, price, side, evidence_source FROM fills WHERE fill_id = ?",
        (fill_id,),
    ).fetchone()
    if row is None or row["evidence_source"] != "cumulative_recovery":
        return None
    return CumulativeRecoveryFill(
        fill_id=row["fill_id"],
        order_ref=row["order_ref"],
        quantity=float(row["qty"]),
        price=float(row["price"]),
        side=row["side"],
    )


def cumulative_recovery_fills_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> tuple[CumulativeRecoveryFill, ...]:
    """Return every active aggregate recovery row for a historical proof check."""
    rows = conn.execute(
        "SELECT fill_id, order_ref, qty, price, side FROM fills "
        "WHERE order_ref = ? AND evidence_source = 'cumulative_recovery' "
        "ORDER BY fill_id ASC",
        (order_ref,),
    ).fetchall()
    return tuple(
        CumulativeRecoveryFill(
            fill_id=row["fill_id"],
            order_ref=row["order_ref"],
            quantity=float(row["qty"]),
            price=float(row["price"]),
            side=row["side"],
        )
        for row in rows
    )


def exact_replaces_cumulative(
    *,
    exact: ExecutionSliceFilledFacts,
    cumulative: CumulativeRecoveryFill,
) -> bool:
    """Temporary S0 one-to-one mirror of the canonical set-proof predicate.

    Formula: same side, |q_cumulative − q_exact| < FILL_QTY_EPSILON shares,
      and |p_cumulative − p_exact| < tick(p_cumulative) per share
      (total_price_conflict_atol, #2791). On a cumulative row that holds only
      part of its order this is stricter than the set proof, which measures
      the gap on the order's whole average.
    Reference: Project-authored S0 operator contract retained during the
      execution-coverage migration in PRD #1543; the price rule is ADR 0036's
      vendor-rounding rule.
    Canonical implementation: prove_execution_coverage_set in this file; this
      S0 predicate remains only for the shipped operator flow.
    Validated against: tests/broker/alpaca/clerk/sqlite/
      test_execution_coverage_set_proof.py::test_s0_one_to_one_predicate_matches_canonical_set_proof_on_unambiguous_inputs.
    """
    return (
        cumulative.side == exact.side
        and abs(cumulative.quantity - exact.slice_qty) < FILL_QTY_EPSILON
        and _is_finite(cumulative.price)
        and abs(cumulative.price - exact.slice_price) < total_price_conflict_atol(cumulative.price)
    )


def _unavailable(
    conflict: ActiveExecutionCoverageConflict,
    quarantined: tuple[ExecutionCoverageQuarantinedFacts, ...],
    reason: str,
    *,
    cumulative: CumulativeRecoveryFill | None = None,
) -> ExecutionCoverageProof:
    return ExecutionCoverageProof(
        conflict=conflict,
        quarantined=quarantined,
        cumulative=cumulative,
        proof_available=False,
        unavailable_reason=reason,
    )
