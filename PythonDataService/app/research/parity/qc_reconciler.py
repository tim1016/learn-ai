"""QC reconciler — diff QC's recorded backtest against our trade log.

Public entry point: :func:`reconcile_qc_aapl_phase3`. Implementation is
split into private functions so each stage is unit-testable:

1. :func:`_parse_qc_orders` — flatten ``qc_orders.json`` events into ``QcFill``.
2. :func:`_audit_fixture`  — verify each QC fill is explained by the
   trading-date bar open within tolerance (gate before alignment).
3. :func:`_align_fills`    — pair QC fills with our fills by
   ``(trading_date, side)``.
4. :func:`_classify_divergences` — walk the tolerance table per pair.

See ``docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md``
for the design rationale, divergence taxonomy, and acceptance gates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as Date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from app.research.parity.fixture_data_reader import FixtureDataReader
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel

Side = Literal["buy", "sell"]


class DivergenceCategory(StrEnum):
    """Categorical divergence types — see numerical-rigor.md."""

    FIXTURE_INSUFFICIENT = "fixture_insufficient"
    DECISION_MISMATCH = "decision_mismatch"
    DIRECTION_MISMATCH = "direction_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"
    FILL_PRICE_DRIFT = "fill_price_drift"
    COMMISSION_DRIFT = "commission_drift"
    PNL_DRIFT = "pnl_drift"
    ORDER_TYPE_MISMATCH = "order_type_mismatch"


@dataclass(frozen=True)
class Tolerances:
    """Per-field comparison tolerances. Phase 3 defaults match the design spec."""

    fill_price_atol: Decimal = Decimal("0.01")
    commission_atol: Decimal = Decimal("0.01")
    per_share_pnl_atol: Decimal = Decimal("0.01")
    pnl_floor_atol: Decimal = Decimal("0.01")

    @classmethod
    def phase3_default(cls) -> Tolerances:
        return cls()


@dataclass(frozen=True)
class QcFill:
    """One fill event extracted from QC's ``/backtests/orders/read`` payload."""

    order_id: int
    symbol: str
    side: Side
    fill_qty: int
    fill_price: Decimal
    fill_time_ms: int
    fee: Decimal | None
    order_type_code: int

    @property
    def trading_date(self) -> Date:
        return datetime.fromtimestamp(self.fill_time_ms / 1000, tz=UTC).date()


@dataclass(frozen=True)
class OurFill:
    """One fill from our engine's trade log, normalized to the reconciler shape."""

    symbol: str
    side: Side
    fill_qty: int
    fill_price: Decimal
    fill_time_ms: int
    fee: Decimal

    @property
    def trading_date(self) -> Date:
        return datetime.fromtimestamp(self.fill_time_ms / 1000, tz=UTC).date()


@dataclass(frozen=True)
class FixtureAudit:
    """A QC fill whose price isn't explainable from the captured bars."""

    qc_fill: QcFill
    reason: str
    expected_open: Decimal | None
    actual_fill_price: Decimal


@dataclass(frozen=True)
class ReconciledPair:
    """A matched ``(QcFill, OurFill)`` pair, or a half-pair when one side is missing."""

    qc: QcFill | None
    ours: OurFill | None
    trading_date: Date
    side: Side | None


@dataclass(frozen=True)
class Divergence:
    """One typed disagreement between paired fills."""

    category: DivergenceCategory
    pair: ReconciledPair
    detail: str


@dataclass(frozen=True)
class ReconciliationSummary:
    n_pairs: int
    n_qc_fills: int
    n_our_fills: int
    n_unmatched_qc: int
    n_unmatched_ours: int
    n_divergences_by_category: dict[DivergenceCategory, int]


@dataclass(frozen=True)
class Diagnostics:
    computed_ibkr_fees: dict[int, Decimal] = field(default_factory=dict)
    propagated_pnl_atol: Decimal = Decimal("0")


@dataclass(frozen=True)
class FixtureMetadata:
    qc_orders_path: Path
    qc_price_history_path: Path
    window_start: Date | None
    window_end: Date | None


@dataclass(frozen=True)
class ReconciliationReport:
    """Top-level result of a reconciliation run."""

    status: Literal["passed", "failed"]
    summary: ReconciliationSummary
    tolerances: Tolerances
    fixture_audit: list[FixtureAudit]
    pairs: list[ReconciledPair]
    divergences: list[Divergence]
    diagnostics: Diagnostics
    fixture_metadata: FixtureMetadata

    def render_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"# QC AAPL Phase 3 reconciliation report — {self.status.upper()}")
        lines.append("")
        lines.append("## Summary")
        s = self.summary
        lines.append(f"- Pairs: {s.n_pairs}")
        lines.append(f"- QC fills: {s.n_qc_fills} | ours: {s.n_our_fills}")
        lines.append(f"- Unmatched QC: {s.n_unmatched_qc} | unmatched ours: {s.n_unmatched_ours}")
        lines.append(f"- Propagated PnL atol: {self.diagnostics.propagated_pnl_atol}")
        for cat, n in s.n_divergences_by_category.items():
            lines.append(f"  - {cat.value}: {n}")
        if self.divergences:
            lines.append("")
            lines.append("## Divergences")
            for d in self.divergences:
                lines.append(f"- [{d.category.value}] {d.pair.trading_date} ({d.pair.side or '?'}): {d.detail}")
        if self.fixture_audit:
            lines.append("")
            lines.append("## Fixture audit failures")
            for fa in self.fixture_audit:
                lines.append(f"- {fa.qc_fill.trading_date}: {fa.reason}")
        lines.append("")
        lines.append("## Fixture")
        lines.append(f"- orders: `{self.fixture_metadata.qc_orders_path}`")
        lines.append(f"- prices: `{self.fixture_metadata.qc_price_history_path}`")
        lines.append(f"- window: {self.fixture_metadata.window_start} → {self.fixture_metadata.window_end}")
        return "\n".join(lines) + "\n"

    def render_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": {
                "n_pairs": self.summary.n_pairs,
                "n_qc_fills": self.summary.n_qc_fills,
                "n_our_fills": self.summary.n_our_fills,
                "n_unmatched_qc": self.summary.n_unmatched_qc,
                "n_unmatched_ours": self.summary.n_unmatched_ours,
                "n_divergences_by_category": {k.value: v for k, v in self.summary.n_divergences_by_category.items()},
            },
            "divergence_count": len(self.divergences),
            "fixture_audit_count": len(self.fixture_audit),
            "propagated_pnl_atol": str(self.diagnostics.propagated_pnl_atol),
        }


def _parse_qc_orders(path: Path) -> list[QcFill]:
    """Flatten QC's ``/backtests/orders/read`` payload into ``QcFill`` rows.

    The payload's exact shape isn't pinned in QC's public docs; this parser
    accepts both ``{"orders": [...]}`` and a top-level list. Each order has
    one or more ``events``; we emit one ``QcFill`` per event so partial fills
    are visible to the alignment step.
    """
    payload = json.loads(Path(path).read_text())
    raw_orders = payload.get("orders") if isinstance(payload, dict) else payload
    if raw_orders is None:
        raw_orders = payload  # tolerate the alternate top-level-list shape
    fills: list[QcFill] = []
    for order in raw_orders:
        symbol = str(order["symbol"]).split(" ", 1)[0]  # strip QC security-id suffix
        order_type_code = int(order.get("type", 0))
        for event in order.get("events", []):
            fill_qty = int(event["fillQuantity"])
            if fill_qty == 0:
                continue  # QC emits zero-quantity book-keeping events; skip them
            side: Side = "buy" if fill_qty > 0 else "sell"
            time_str = str(event["time"])
            event_dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            fee_raw = event.get("orderFeeAmount")
            fills.append(
                QcFill(
                    order_id=int(order["id"]),
                    symbol=symbol,
                    side=side,
                    fill_qty=fill_qty,
                    fill_price=Decimal(str(event["fillPrice"])),
                    fill_time_ms=int(event_dt.timestamp() * 1000),
                    fee=None if fee_raw is None else Decimal(str(fee_raw)),
                    order_type_code=order_type_code,
                )
            )
    return fills


def _audit_fixture(
    qc_fills: list[QcFill],
    reader: FixtureDataReader,
    tolerances: Tolerances,
) -> list[FixtureAudit]:
    """Check that each QC fill is explained by the same trading-date's bar open.

    Returns one ``FixtureAudit`` per unexplained fill (empty when the
    fixture is internally consistent). Mismatches mean the captured price
    history is missing the resolution required to reproduce QC's fill —
    Phase 3.5 escalation rather than a Phase 3 engine bug.
    """
    if not qc_fills:
        return []
    symbol = qc_fills[0].symbol
    opens = reader.bar_open_by_date(symbol)
    audits: list[FixtureAudit] = []
    for qc in qc_fills:
        bar_open = opens.get(qc.trading_date)
        if bar_open is None:
            audits.append(
                FixtureAudit(
                    qc_fill=qc,
                    reason=f"no bar in fixture for trading date {qc.trading_date}",
                    expected_open=None,
                    actual_fill_price=qc.fill_price,
                )
            )
            continue
        if abs(bar_open - qc.fill_price) > tolerances.fill_price_atol:
            audits.append(
                FixtureAudit(
                    qc_fill=qc,
                    reason=(
                        f"fill {qc.fill_price} not explained by bar open {bar_open} "
                        f"(tolerance {tolerances.fill_price_atol})"
                    ),
                    expected_open=bar_open,
                    actual_fill_price=qc.fill_price,
                )
            )
    return audits


def _align_fills(
    qc_fills: list[QcFill],
    our_fills: list[OurFill],
) -> list[ReconciledPair]:
    """Pair QC fills with ours by ``(trading_date, side)``.

    Daily AAPL → at most one fill per (date, side). Anything unmatched on
    either side surfaces as a half-pair, which ``_classify_divergences``
    later turns into a ``DECISION_MISMATCH``.
    """
    qc_map: dict[tuple[Side, Date], QcFill] = {(f.side, f.trading_date): f for f in qc_fills}
    ours_map: dict[tuple[Side, Date], OurFill] = {(f.side, f.trading_date): f for f in our_fills}
    all_keys = sorted(set(qc_map) | set(ours_map), key=lambda x: (x[1], x[0]))
    return [
        ReconciledPair(
            qc=qc_map.get(k),
            ours=ours_map.get(k),
            trading_date=k[1],
            side=k[0],
        )
        for k in all_keys
    ]


def _classify_divergences(
    pairs: list[ReconciledPair],
    tolerances: Tolerances,
    *,
    assert_fees: bool,
    computed_ibkr_fees: dict[int, Decimal] | None = None,
) -> list[Divergence]:
    """Emit zero or more typed divergences per pair."""
    out: list[Divergence] = []
    fees_by_order = computed_ibkr_fees or {}
    for pair in pairs:
        if pair.qc is None or pair.ours is None:
            out.append(
                Divergence(
                    category=DivergenceCategory.DECISION_MISMATCH,
                    pair=pair,
                    detail=(
                        f"only one side has a fill on {pair.trading_date} "
                        f"({pair.side}); qc={pair.qc is not None}, ours={pair.ours is not None}"
                    ),
                )
            )
            continue
        qc, ours = pair.qc, pair.ours
        if qc.side != ours.side:
            out.append(
                Divergence(
                    category=DivergenceCategory.DIRECTION_MISMATCH,
                    pair=pair,
                    detail=f"qc={qc.side} ours={ours.side}",
                )
            )
        if qc.fill_qty != ours.fill_qty:
            out.append(
                Divergence(
                    category=DivergenceCategory.QUANTITY_MISMATCH,
                    pair=pair,
                    detail=f"qc qty={qc.fill_qty} ours qty={ours.fill_qty}",
                )
            )
        if abs(qc.fill_price - ours.fill_price) > tolerances.fill_price_atol:
            out.append(
                Divergence(
                    category=DivergenceCategory.FILL_PRICE_DRIFT,
                    pair=pair,
                    detail=(f"|{qc.fill_price} - {ours.fill_price}| > {tolerances.fill_price_atol}"),
                )
            )
        if assert_fees and qc.fee is not None:
            expected = fees_by_order.get(qc.order_id)
            if expected is not None and abs(qc.fee - expected) > tolerances.commission_atol:
                out.append(
                    Divergence(
                        category=DivergenceCategory.COMMISSION_DRIFT,
                        pair=pair,
                        detail=f"qc fee={qc.fee} expected ibkr={expected}",
                    )
                )
        if qc.order_type_code != 0:
            out.append(
                Divergence(
                    category=DivergenceCategory.ORDER_TYPE_MISMATCH,
                    pair=pair,
                    detail=f"qc order_type={qc.order_type_code} (expected market=0)",
                )
            )
    return out


def reconcile_qc_aapl_phase3(
    *,
    qc_orders_path: Path,
    qc_price_history_path: Path,
    our_fills: list[OurFill],
    tolerances: Tolerances | None = None,
    assert_fees: bool = False,
) -> ReconciliationReport:
    """Reconcile QC's recorded backtest against ours and return a typed report.

    ``assert_fees`` toggles ``COMMISSION_DRIFT`` as a gating category. Set to
    ``True`` only after the capture-smoke step (see Phase 3 spec §2.1.2)
    confirms QC's payload contains non-zero ``orderFeeAmount`` values
    (Branch A); leave ``False`` for Branch B fixtures where fees are
    informational only.
    """
    tolerances = tolerances or Tolerances.phase3_default()
    qc_fills = _parse_qc_orders(qc_orders_path)
    reader = FixtureDataReader(csv_path=qc_price_history_path)

    audit = _audit_fixture(qc_fills, reader, tolerances)

    commission_model = IbkrEquityCommissionModel()
    computed_fees: dict[int, Decimal] = {
        qf.order_id: commission_model.fee(quantity=qf.fill_qty, fill_price=qf.fill_price) for qf in qc_fills
    }

    pairs = _align_fills(qc_fills, our_fills)
    if audit:
        # Fixture itself doesn't explain QC — emit FIXTURE_INSUFFICIENT
        # and skip the rest of the classification: pair-level divergences
        # would be misleading when the input data is suspect.
        divergences: list[Divergence] = [
            Divergence(
                category=DivergenceCategory.FIXTURE_INSUFFICIENT,
                pair=ReconciledPair(
                    qc=fa.qc_fill,
                    ours=None,
                    trading_date=fa.qc_fill.trading_date,
                    side=fa.qc_fill.side,
                ),
                detail=fa.reason,
            )
            for fa in audit
        ]
    else:
        divergences = _classify_divergences(
            pairs,
            tolerances,
            assert_fees=assert_fees,
            computed_ibkr_fees=computed_fees,
        )

    total_qty = sum(abs(f.fill_qty) for f in qc_fills)
    n_fills = len(qc_fills)
    propagated_pnl_atol = (
        Decimal(total_qty) * tolerances.per_share_pnl_atol + Decimal(n_fills) * tolerances.commission_atol
        if n_fills
        else Decimal("0")
    )

    counts: dict[DivergenceCategory, int] = {}
    for d in divergences:
        counts[d.category] = counts.get(d.category, 0) + 1

    summary = ReconciliationSummary(
        n_pairs=len(pairs),
        n_qc_fills=len(qc_fills),
        n_our_fills=len(our_fills),
        n_unmatched_qc=sum(1 for p in pairs if p.qc is not None and p.ours is None),
        n_unmatched_ours=sum(1 for p in pairs if p.qc is None and p.ours is not None),
        n_divergences_by_category=counts,
    )

    gating: set[DivergenceCategory] = {
        DivergenceCategory.FIXTURE_INSUFFICIENT,
        DivergenceCategory.DECISION_MISMATCH,
        DivergenceCategory.DIRECTION_MISMATCH,
        DivergenceCategory.QUANTITY_MISMATCH,
        DivergenceCategory.FILL_PRICE_DRIFT,
        DivergenceCategory.ORDER_TYPE_MISMATCH,
        DivergenceCategory.PNL_DRIFT,
    }
    if assert_fees:
        gating.add(DivergenceCategory.COMMISSION_DRIFT)

    status: Literal["passed", "failed"] = "passed" if not any(d.category in gating for d in divergences) else "failed"

    metadata = FixtureMetadata(
        qc_orders_path=Path(qc_orders_path),
        qc_price_history_path=Path(qc_price_history_path),
        window_start=min((f.trading_date for f in qc_fills), default=None),
        window_end=max((f.trading_date for f in qc_fills), default=None),
    )
    diagnostics = Diagnostics(
        computed_ibkr_fees=computed_fees,
        propagated_pnl_atol=propagated_pnl_atol,
    )
    return ReconciliationReport(
        status=status,
        summary=summary,
        tolerances=tolerances,
        fixture_audit=audit,
        pairs=pairs,
        divergences=divergences,
        diagnostics=diagnostics,
        fixture_metadata=metadata,
    )


__all__ = [
    "Diagnostics",
    "Divergence",
    "DivergenceCategory",
    "FixtureAudit",
    "FixtureMetadata",
    "OurFill",
    "QcFill",
    "ReconciledPair",
    "ReconciliationReport",
    "ReconciliationSummary",
    "Tolerances",
    "reconcile_qc_aapl_phase3",
]
