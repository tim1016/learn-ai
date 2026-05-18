"""LEAN Sidecar Phase 5a — self-reconciliation against the IBKR commission model.

Takes the normalized result of any past LEAN Lab run and compares each
filled order event's recorded ``orderFeeAmount`` against the fee that
the canonical :class:`IbkrEquityCommissionModel` would have computed.
Produces a categorized divergence report that uses the same
``commission_drift`` semantics as the QC reconciler taxonomy in
``.claude/rules/numerical-rigor.md`` § "Trade-level reconciliation
taxonomy".

What this is NOT:
- It is not a trade-by-trade reconciliation against an external
  reference engine (that's Phase 5c, the LEAN-Lab-vs-Engine-Lab
  reconciler). This module compares LEAN against a model, not LEAN
  against another engine's trades.
- It does not modify the run, only reads its normalized result.
- It cannot tell you whether the run was reconciliation-grade — a
  default-brokerage run will naturally surface many ``commission_drift``
  rows because LEAN's default commission ≠ IBKR's tier. That signal is
  informative (it shows the brokerage choice matters) but not a bug.

Authority for the IBKR model: ``app/research/parity/ibkr_commission.py``
(this file does not redefine the math, it consumes it via the existing
``IbkrEquityCommissionModel`` dataclass).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.lean_sidecar.normalized_parser import NormalizedOrderEvent
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel

# Money on this layer is cents-precise. The IBKR model already
# quantizes to cents internally; recorded fees come in as Python
# floats and need explicit quantization so the serialized wire form
# is always "1.00" / "5.00", not "1.0" / "5".
_CENTS = Decimal("0.01")


def _to_cents(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)


# Default tolerance matches the QC reconciler's ``commission_atol`` from
# numerical-rigor.md so a divergence here is comparable to a divergence
# there: $0.01 — the IBKR fee is reported and computed to the cent.
DEFAULT_COMMISSION_ATOL = Decimal("0.01")

# LEAN's filled-event status string. The normalized parser preserves
# LEAN's casing; we match case-insensitively because LEAN has been
# inconsistent across versions ("Filled" vs "filled").
_FILLED_STATUS = "filled"


class FeeDivergenceCategory(StrEnum):
    """Categories the reconciler can emit. Strict subset of the project-
    wide :class:`DivergenceCategory` so consumers can lift these into the
    broader taxonomy without translation.
    """

    COMMISSION_DRIFT = "commission_drift"
    # LEAN emitted a filled event but did not record an order fee. Not
    # a model-vs-recorded comparison (we have nothing to compare); just
    # surfaced so the operator can see the gap.
    NO_RECORDED_FEE = "no_recorded_fee"
    # LEAN emitted a fractional-share fill quantity (e.g., 100.5). The
    # IBKR equity-tier model is integer-shares only — rounding silently
    # would let drift hide behind ``round()``'s banker semantics
    # (``round(100.5) == 100`` in Python 3). The reconciler classifies
    # these as their own category so the operator decides how to handle
    # them (Phase 5b+ will add a fractional-share commission model or
    # filter fractional fills upstream).
    FRACTIONAL_QUANTITY = "fractional_quantity"


@dataclass(frozen=True, slots=True)
class FeeDivergence:
    """One filled order event whose recorded fee disagrees with the model.

    ``delta`` is ``recorded - expected``. Positive: LEAN recorded more
    than IBKR would charge. Negative: LEAN under-charged versus IBKR.

    ``fill_quantity_raw`` carries the original float quantity when the
    fill was fractional (category ``FRACTIONAL_QUANTITY``); ``None``
    for integer-share fills. The integer ``fill_quantity`` is the
    rounded value the IBKR model would have charged against if we
    had not bailed — kept so the operator can spot the gap visually.
    """

    order_event_id: int
    order_id: int
    symbol: str
    ms_utc: int
    fill_quantity: int
    fill_price: Decimal
    recorded_fee: Decimal | None
    expected_ibkr_fee: Decimal
    delta: Decimal | None
    category: FeeDivergenceCategory
    fill_quantity_raw: float | None = None


@dataclass(frozen=True, slots=True)
class FeeReconciliationReport:
    """One report per LEAN Lab run.

    ``matched_count`` + ``divergent_count`` equals the count of filled
    events considered. Non-fill events (Submitted, Cancelled, etc.) are
    excluded — they have no fee to compare.
    """

    run_id: str
    total_fill_events: int
    matched_count: int
    divergent_count: int
    divergences: tuple[FeeDivergence, ...]
    total_recorded_fees: Decimal
    total_expected_ibkr_fees: Decimal
    commission_atol: Decimal


def _is_filled(event: NormalizedOrderEvent) -> bool:
    """Filled events are the only ones the IBKR model has a referent for."""
    return event.status.casefold() == _FILLED_STATUS


def _reconcile_one(
    event: NormalizedOrderEvent,
    model: IbkrEquityCommissionModel,
    commission_atol: Decimal,
) -> tuple[Decimal, FeeDivergence | None]:
    """Reconcile one filled event; return (expected_fee, divergence_or_None).

    ``fill_quantity`` is a float in the wire model (LEAN uses doubles)
    but the IBKR model takes int. Fractional-share fills (100.5 shares)
    can't be reconciled against an integer-shares model — silently
    rounding lets drift hide behind ``round()``'s banker semantics
    (``round(100.5) == 100`` in Python 3, so a fractional fill could
    erroneously reconcile clean). The reconciler classifies fractional
    fills as their own ``FRACTIONAL_QUANTITY`` category instead.
    ``fill_price`` similarly comes in as a float and is converted to
    Decimal via str() so we don't pick up float-binary error.
    """
    fill_price_d = Decimal(str(event.fill_price))
    # Detect fractional quantity BEFORE rounding so 100.5 surfaces as
    # FRACTIONAL_QUANTITY instead of silently reconciling against the
    # rounded-to-100 IBKR fee. ``int(x) != x`` is the right check
    # (rather than ``x != round(x)``) because it doesn't depend on
    # banker-rounding semantics — a float that is exactly integral
    # (100.0, -50.0) passes; anything with a true fractional part fails.
    is_fractional = float(event.fill_quantity) != int(event.fill_quantity)
    if is_fractional:
        # IBKR expected fee is undefined for fractional fills against
        # the integer-shares model; Decimal("0.00") is a placeholder
        # so the field stays typed. ``delta=None`` because there is no
        # meaningful comparison to make.
        divergence = FeeDivergence(
            order_event_id=event.order_event_id,
            order_id=event.order_id,
            symbol=event.symbol_value,
            ms_utc=event.ms_utc,
            fill_quantity=int(event.fill_quantity),
            fill_price=fill_price_d,
            recorded_fee=None if event.order_fee_amount is None else _to_cents(event.order_fee_amount),
            expected_ibkr_fee=Decimal("0.00"),
            delta=None,
            category=FeeDivergenceCategory.FRACTIONAL_QUANTITY,
            fill_quantity_raw=float(event.fill_quantity),
        )
        # Don't add the placeholder Decimal("0.00") to the expected-fee
        # total — it would falsify the aggregate. Return zero so the
        # caller's accumulator is a no-op for this row.
        return Decimal("0.00"), divergence
    rounded_qty = int(event.fill_quantity)
    expected = model.fee(
        quantity=rounded_qty,
        fill_price=fill_price_d,
    )
    recorded = None if event.order_fee_amount is None else _to_cents(event.order_fee_amount)
    if recorded is None:
        divergence = FeeDivergence(
            order_event_id=event.order_event_id,
            order_id=event.order_id,
            symbol=event.symbol_value,
            ms_utc=event.ms_utc,
            fill_quantity=rounded_qty,
            fill_price=fill_price_d,
            recorded_fee=None,
            expected_ibkr_fee=expected,
            delta=None,
            category=FeeDivergenceCategory.NO_RECORDED_FEE,
        )
        return expected, divergence
    delta = (recorded - expected).quantize(_CENTS, rounding=ROUND_HALF_UP)
    if abs(delta) <= commission_atol:
        return expected, None
    return expected, FeeDivergence(
        order_event_id=event.order_event_id,
        order_id=event.order_id,
        symbol=event.symbol_value,
        ms_utc=event.ms_utc,
        fill_quantity=rounded_qty,
        fill_price=fill_price_d,
        recorded_fee=recorded,
        expected_ibkr_fee=expected,
        delta=delta,
        category=FeeDivergenceCategory.COMMISSION_DRIFT,
    )


def reconcile_against_ibkr(
    run_id: str,
    order_events: Iterable[NormalizedOrderEvent],
    *,
    commission_atol: Decimal = DEFAULT_COMMISSION_ATOL,
    model: IbkrEquityCommissionModel | None = None,
) -> FeeReconciliationReport:
    """Reconcile a run's recorded fees against the IBKR equity tier model.

    The default tolerance ($0.01) matches numerical-rigor.md so a clean
    report here is directly comparable to a clean Engine-Lab-vs-QC
    report. Operators may pass a looser ``commission_atol`` for
    diagnostic-only reconciliations of non-IBKR runs but the tolerance
    is documented in the report so a reader can tell.
    """
    if model is None:
        model = IbkrEquityCommissionModel()
    fills = [e for e in order_events if _is_filled(e)]
    divergences: list[FeeDivergence] = []
    total_recorded = Decimal("0.00")
    total_expected = Decimal("0.00")
    matched = 0
    for event in fills:
        expected, divergence = _reconcile_one(event, model, commission_atol)
        total_expected += expected
        if event.order_fee_amount is not None:
            total_recorded += _to_cents(event.order_fee_amount)
        if divergence is None:
            matched += 1
        else:
            divergences.append(divergence)
    return FeeReconciliationReport(
        run_id=run_id,
        total_fill_events=len(fills),
        matched_count=matched,
        divergent_count=len(divergences),
        divergences=tuple(divergences),
        total_recorded_fees=total_recorded,
        total_expected_ibkr_fees=total_expected,
        commission_atol=commission_atol,
    )


__all__ = [
    "DEFAULT_COMMISSION_ATOL",
    "FeeDivergence",
    "FeeDivergenceCategory",
    "FeeReconciliationReport",
    "reconcile_against_ibkr",
]
