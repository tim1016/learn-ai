"""Phase 5g.3 — cross-engine fill-by-fill comparator.

Consumes the two sides:

* **LEAN-Lab side** — ``NormalizedOrderEvent`` instances from the parsed
  ``result.json`` (Phase 3a parser output).
* **Engine-Lab side** — ``CrossRunOrderEvent`` instances from
  ``cross_runner.run_engine_lab_on_workspace`` (Phase 5g.2 primitive).

Pairs fills by ``(NY-trading-date, direction)``, classifies disagreements
into :class:`app.research.parity.qc_reconciler.DivergenceCategory`, and
returns a structured output the router can fold into
``CrossEngineReconciliationReportModel`` without further translation.

Default gating per mission-critical doc D3:

  Gating: ``DECISION_MISMATCH``, ``DIRECTION_MISMATCH``,
  ``QUANTITY_MISMATCH``, ``FILL_PRICE_DRIFT``, ``ORDER_TYPE_MISMATCH``,
  ``PNL_DRIFT``, ``FIXTURE_INSUFFICIENT``.
  Diagnostic-only by default: ``COMMISSION_DRIFT``.

When the caller passes ``assert_fees=True`` (Branch-A semantics —
meaningful only on reconciliation-grade templates where both engines
pin IBKR fees), ``COMMISSION_DRIFT`` is promoted to gating.

What this module does NOT compute:

* ``FIXTURE_INSUFFICIENT`` — both engines ran on the same workspace
  data zips (D3 shared staged data), so price-explainability audits
  are not applicable. The category remains in the gating set so that
  if someone wires it later, the gating-set invariant is preserved.
* ``PNL_DRIFT`` — Phase 5g.3 is a fill-level diff. Round-trip pairing
  + realized-P&L reconciliation are out of scope for this slice. A
  future Phase 5g.x can re-use ``qc_reconciler._pair_round_trips`` if
  needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from app.lean_sidecar.cross_runner import CrossRunOrderEvent
from app.lean_sidecar.normalized_parser import NormalizedOrderEvent
from app.research.parity.qc_reconciler import DivergenceCategory

_NY = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

Side = Literal["Buy", "Sell"]


@dataclass(frozen=True)
class CrossReconciliationTolerances:
    """Per-field tolerances. Defaults follow ``numerical-rigor.md``."""

    fill_price_atol: Decimal = Decimal("0.01")
    commission_atol: Decimal = Decimal("0.01")
    # ``Decimal`` (not ``int``) so the comparator's
    # ``abs(lean - engine) > qty_atol`` works on Decimal-typed sides
    # produced by the adapters. Default 0 — strict equality — so any
    # fractional drift surfaces unless the caller explicitly widens.
    qty_atol: Decimal = Decimal(0)

    @classmethod
    def default(cls) -> CrossReconciliationTolerances:
        return cls()


@dataclass(frozen=True)
class _InternalFill:
    """Common internal shape both LEAN and Engine sides adapt into.

    Pairing key is ``(trading_date, side)``. ``side`` here is the
    LEAN/Engine direction string already — no Buy/Sell→side
    translation happens later, so the comparator works on
    wire-identical strings.

    ``fill_quantity`` is ``Decimal`` (not ``int``) because LEAN can
    emit fractional-share fills (e.g., ``100.5``). Engines that disagree
    only on the fractional part previously compared equal because the
    adapter ``int``-truncated both sides before comparison; using
    Decimal throughout preserves the divergence.
    """

    side: Side
    fill_quantity: Decimal  # unsigned magnitude; sign lives in ``side``
    fill_price: Decimal
    fill_time_ms_utc: int
    fee: Decimal | None
    symbol: str

    @property
    def trading_date(self) -> date:
        return datetime.fromtimestamp(
            self.fill_time_ms_utc / 1000, tz=_NY
        ).date()


@dataclass(frozen=True)
class CrossDivergence:
    """One typed disagreement between paired fills."""

    category: DivergenceCategory
    trading_date: date
    detail: str
    lean_fill: _InternalFill | None
    engine_fill: _InternalFill | None


@dataclass(frozen=True)
class CrossReconciliationOutput:
    """Structured comparator result the router folds into the wire model."""

    lean_total_fills: int
    engine_total_fills: int
    matched_count: int
    divergent_count: int
    gating_divergent_count: int
    passed: bool
    counts_by_category: dict[DivergenceCategory, int]
    divergences: list[CrossDivergence] = field(default_factory=list)


# LEAN emits ``status`` + ``direction`` in lowercase on the wire
# (``"filled"`` / ``"buy"`` / ``"sell"``); the Phase 3a parser passes
# them through unchanged. Comparisons use ``.casefold()`` (matching the
# existing pattern in :mod:`app.lean_sidecar.reconciler`) so case
# variants — including Unicode — fold to the canonical form. Side is
# normalized to the capitalized wire form (``"Buy"`` / ``"Sell"``) the
# cross-reconciler's response model and the paired qc_reconciler
# taxonomy both use.


def _adapt_lean_event(event: NormalizedOrderEvent) -> _InternalFill | None:
    """Adapt a LEAN ``NormalizedOrderEvent`` to the internal shape.

    Returns ``None`` if the event is not a fill (a ``submitted``
    lifecycle event) or has zero fill_quantity. Fee may be absent
    (``None``) when the LEAN run did not record ``orderFeeAmount``.

    Case-folds ``status`` and ``direction`` so the adapter is robust to
    LEAN presentation changes — same pattern as
    :mod:`app.lean_sidecar.reconciler`. Quantity is preserved as
    ``Decimal`` (review-fix): truncating LEAN's float quantity to int
    hides fractional-share fills, and two engines that disagreed only
    on the fractional part would have compared equal.
    """
    if event.status.casefold() != "filled":
        return None
    qty_decimal = Decimal(str(event.fill_quantity))
    if qty_decimal == 0:
        return None
    direction_cf = event.direction.casefold()
    if direction_cf == "buy":
        side: Side = "Buy"
    elif direction_cf == "sell":
        side = "Sell"
    else:
        # Unknown direction — parser-version-skew or LEAN-behavior-
        # change signal. Surface as a dropped fill rather than silently
        # coercing.
        return None
    fee = (
        Decimal(str(event.order_fee_amount))
        if event.order_fee_amount is not None
        else None
    )
    return _InternalFill(
        side=side,
        fill_quantity=abs(qty_decimal),
        fill_price=Decimal(str(event.fill_price)),
        fill_time_ms_utc=int(event.ms_utc),
        fee=fee,
        symbol=event.symbol.upper(),
    )


def _adapt_engine_event(event: CrossRunOrderEvent) -> _InternalFill:
    """Adapt an Engine-Lab ``CrossRunOrderEvent`` to the internal shape.

    The engine's ``CrossRunOrderEvent.fill_quantity`` is already an
    unsigned ``int`` — Engine Lab's ``OrderEvent`` cannot represent
    fractional shares — but the internal shape uses ``Decimal`` so the
    comparator's subtraction stays in one type domain and surfaces any
    LEAN-side fractional drift cleanly.
    """
    return _InternalFill(
        side=event.direction,
        fill_quantity=Decimal(event.fill_quantity),
        fill_price=event.fill_price,
        fill_time_ms_utc=event.ms_utc,
        fee=event.fee,
        symbol=event.symbol.upper(),
    )


def _pair_fills(
    lean_fills: list[_InternalFill],
    engine_fills: list[_InternalFill],
) -> list[tuple[date, Side, _InternalFill | None, _InternalFill | None]]:
    """Pair fills by ``(trading_date, side)``.

    When a date+side has multiple fills on one engine but a single fill
    on the other, only the first pair is matched and subsequent fills
    on the dominant side become unpaired (``DECISION_MISMATCH`` rows
    for the missing-side records). Phase 5g.3 doesn't try to multi-pair
    inside the same date+side bucket — both engines doing buy-and-hold-
    style algorithms should produce at most one entry + one exit per
    date+side bucket; anything beyond that is an unusual case better
    surfaced explicitly than silently averaged.
    """
    by_key_lean: dict[tuple[date, Side], list[_InternalFill]] = {}
    by_key_engine: dict[tuple[date, Side], list[_InternalFill]] = {}
    for f in lean_fills:
        by_key_lean.setdefault((f.trading_date, f.side), []).append(f)
    for f in engine_fills:
        by_key_engine.setdefault((f.trading_date, f.side), []).append(f)
    all_keys = sorted(set(by_key_lean) | set(by_key_engine), key=lambda k: (k[0], k[1]))
    pairs: list[tuple[date, Side, _InternalFill | None, _InternalFill | None]] = []
    for key in all_keys:
        lean_list = by_key_lean.get(key, [])
        engine_list = by_key_engine.get(key, [])
        n = max(len(lean_list), len(engine_list))
        for i in range(n):
            lean_f = lean_list[i] if i < len(lean_list) else None
            engine_f = engine_list[i] if i < len(engine_list) else None
            pairs.append((key[0], key[1], lean_f, engine_f))
    return pairs


# Default gating per D3: every category gating EXCEPT COMMISSION_DRIFT.
# The assert_fees=True override promotes COMMISSION_DRIFT into the set.
_DEFAULT_GATING_CATEGORIES: frozenset[DivergenceCategory] = frozenset(
    {
        DivergenceCategory.FIXTURE_INSUFFICIENT,
        DivergenceCategory.DECISION_MISMATCH,
        DivergenceCategory.DIRECTION_MISMATCH,
        DivergenceCategory.QUANTITY_MISMATCH,
        DivergenceCategory.FILL_PRICE_DRIFT,
        DivergenceCategory.ORDER_TYPE_MISMATCH,
        DivergenceCategory.PNL_DRIFT,
    }
)


def _gating_set(*, assert_fees: bool) -> frozenset[DivergenceCategory]:
    if assert_fees:
        return _DEFAULT_GATING_CATEGORIES | {DivergenceCategory.COMMISSION_DRIFT}
    return _DEFAULT_GATING_CATEGORIES


def compare_cross_engine(
    lean_events: list[NormalizedOrderEvent],
    engine_events: list[CrossRunOrderEvent],
    *,
    tolerances: CrossReconciliationTolerances | None = None,
    assert_fees: bool = False,
) -> CrossReconciliationOutput:
    """Run the cross-engine fill comparator.

    Both sides are filtered to filled events, adapted to a common
    internal shape, paired by ``(NY-trading-date, side)``, then
    classified into ``DivergenceCategory`` rows.

    ``passed`` is True iff zero divergences land in the gating set
    (default-strict per D3; ``COMMISSION_DRIFT`` joins the set when
    ``assert_fees=True``).

    Returns ``CrossReconciliationOutput`` — a router-agnostic shape;
    the endpoint folds it into ``CrossEngineReconciliationReportModel``.
    """
    tols = tolerances or CrossReconciliationTolerances.default()
    lean_fills = [adapted for e in lean_events if (adapted := _adapt_lean_event(e)) is not None]
    engine_fills = [_adapt_engine_event(e) for e in engine_events]
    pairs = _pair_fills(lean_fills, engine_fills)
    gating = _gating_set(assert_fees=assert_fees)

    divergences: list[CrossDivergence] = []
    matched_count = 0
    for trading_date, side, lean_f, engine_f in pairs:
        if lean_f is None or engine_f is None:
            divergences.append(
                CrossDivergence(
                    category=DivergenceCategory.DECISION_MISMATCH,
                    trading_date=trading_date,
                    detail=(
                        f"only one side has a fill on {trading_date} ({side}); "
                        f"lean={lean_f is not None}, engine={engine_f is not None}"
                    ),
                    lean_fill=lean_f,
                    engine_fill=engine_f,
                )
            )
            continue
        # Both sides present — count as a successful pairing even when
        # downstream comparisons surface drift rows. A pair with
        # only FILL_PRICE_DRIFT still represents agreement on the
        # decision and the direction, just not on the fill price.
        matched_count += 1
        # DIRECTION_MISMATCH cannot occur here by construction (we
        # paired ON side), but the qc_reconciler taxonomy carries it
        # as a real category for code paths that pair differently. Skip
        # the check for cross-engine fills paired on (date, side).
        if abs(lean_f.fill_quantity - engine_f.fill_quantity) > tols.qty_atol:
            divergences.append(
                CrossDivergence(
                    category=DivergenceCategory.QUANTITY_MISMATCH,
                    trading_date=trading_date,
                    detail=(
                        f"lean qty={lean_f.fill_quantity} "
                        f"engine qty={engine_f.fill_quantity}"
                    ),
                    lean_fill=lean_f,
                    engine_fill=engine_f,
                )
            )
        if abs(lean_f.fill_price - engine_f.fill_price) > tols.fill_price_atol:
            divergences.append(
                CrossDivergence(
                    category=DivergenceCategory.FILL_PRICE_DRIFT,
                    trading_date=trading_date,
                    detail=(
                        f"|{lean_f.fill_price} - {engine_f.fill_price}| > "
                        f"{tols.fill_price_atol}"
                    ),
                    lean_fill=lean_f,
                    engine_fill=engine_f,
                )
            )
        if lean_f.fee is not None:
            fee_delta = abs(lean_f.fee - engine_f.fee)
            if fee_delta > tols.commission_atol:
                divergences.append(
                    CrossDivergence(
                        category=DivergenceCategory.COMMISSION_DRIFT,
                        trading_date=trading_date,
                        detail=(
                            f"|{lean_f.fee} - {engine_f.fee}| > "
                            f"{tols.commission_atol}"
                        ),
                        lean_fill=lean_f,
                        engine_fill=engine_f,
                    )
                )

    counts_by_category: dict[DivergenceCategory, int] = {}
    for d in divergences:
        counts_by_category[d.category] = counts_by_category.get(d.category, 0) + 1
    gating_divergent_count = sum(
        n for cat, n in counts_by_category.items() if cat in gating
    )

    return CrossReconciliationOutput(
        lean_total_fills=len(lean_fills),
        engine_total_fills=len(engine_fills),
        matched_count=matched_count,
        divergent_count=len(divergences),
        gating_divergent_count=gating_divergent_count,
        passed=gating_divergent_count == 0,
        counts_by_category=counts_by_category,
        divergences=divergences,
    )


def internal_fill_to_dict(fill: _InternalFill, *, ms_to_utc: bool = True) -> dict:
    """Render an internal fill to a dict the router can spread into
    ``CrossEngineFillSnapshotModel``.

    ``fill_price`` and ``fee`` come out as strings — cent-exact wire
    matching the rest of the lean_sidecar surface.

    ``fill_quantity`` is rendered as ``int`` (truncated) so the existing
    UI keeps rendering whole-share counts unchanged. When the internal
    Decimal quantity is fractional (LEAN can emit ``100.5``-style
    fills), the full precision goes into ``fill_quantity_raw`` as a
    string — mirroring the Phase 5a fee reconciler's
    ``fill_quantity_raw`` convention. Consumers ignoring the new field
    keep the old int-only behavior; consumers that care see the exact
    value.
    """
    qty_decimal = fill.fill_quantity
    qty_int = int(qty_decimal)
    raw = (
        str(qty_decimal)
        if Decimal(qty_int) != qty_decimal
        else None
    )
    return {
        "symbol": fill.symbol,
        "side": fill.side,
        "fill_quantity": qty_int,
        "fill_quantity_raw": raw,
        "fill_price": str(fill.fill_price),
        "fill_time_ms_utc": int(fill.fill_time_ms_utc),
        "fee": None if fill.fee is None else str(fill.fee),
    }
