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
class FillRoundTrip:
    """One entry-exit fill pair with computed realized P&L.

    Phase 3 assumes single-position-at-a-time long-only trading
    (StrategySpec entry = ``qc_pred > 0``, exit = ``qc_pred <= 0``):
    a buy fill opens, the next sell fill on a strictly later date
    closes. Short or pyramided patterns are rejected by
    ``_pair_round_trips``.
    """

    entry_trading_date: Date
    entry_qty: int
    entry_price: Decimal
    entry_fee: Decimal
    exit_trading_date: Date
    exit_qty: int
    exit_price: Decimal
    exit_fee: Decimal
    realized_pnl: Decimal
    propagated_atol: Decimal


@dataclass(frozen=True)
class Diagnostics:
    computed_ibkr_fees: dict[int, Decimal] = field(default_factory=dict)
    propagated_pnl_atol: Decimal = Decimal("0")
    qc_round_trips: list[FillRoundTrip] = field(default_factory=list)
    our_round_trips: list[FillRoundTrip] = field(default_factory=list)


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


class FixtureSchemaError(ValueError):
    """Raised when a captured QC fixture does not match the canonical schema.

    The reconciler does *not* try to be tolerant of every QC API response
    shape — the runbook's normalization step is responsible for producing a
    canonical payload, and this parser enforces that contract by failing
    fast on deviations. See
    ``docs/references/qc-aapl-phase3-capture-runbook.md`` § "Canonical
    fixture schema".
    """


def _parse_qc_orders(path: Path) -> list[QcFill]:
    """Flatten QC's normalized orders fixture into ``QcFill`` rows.

    **Canonical schema** (the only shape this parser accepts):

    ```json
    {
      "orders": [
        {
          "id": <int>,
          "symbol": "<str>",         // bare ticker; security-id suffix permitted
          "type": <int>,              // 0 = market
          "events": [
            {
              "time": "<ISO-8601 with Z>" | <int ms since epoch>,
              "fillQuantity": <int>,  // signed; positive=buy, negative=sell
              "fillPrice": <number>,
              "direction": <int>,     // 0=buy, 1=sell (informational)
              "orderFeeAmount": <number|null>
            }
          ]
        }
      ]
    }
    ```

    Anything else is a fixture-shape bug. The runbook's normalization cell
    is responsible for adapting QC's raw API response (which can have
    ``orderEvents`` / nested ``symbol`` / numeric times in seconds) to this
    canonical shape before the fixture is committed.

    Zero-quantity events are skipped (QC emits them as order book-keeping).
    """
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict) or "orders" not in payload:
        raise FixtureSchemaError(
            f"{path}: missing top-level 'orders' key — see canonical schema in _parse_qc_orders docstring"
        )
    raw_orders = payload["orders"]
    if not isinstance(raw_orders, list):
        raise FixtureSchemaError(f"{path}: 'orders' must be a JSON array, got {type(raw_orders).__name__}")
    fills: list[QcFill] = []
    for idx, order in enumerate(raw_orders):
        if "events" not in order:
            raise FixtureSchemaError(
                f"{path}: order #{idx} missing 'events' key. If the raw QC payload uses "
                "'orderEvents', normalize to 'events' in the capture runbook before committing."
            )
        if not isinstance(order.get("symbol"), str):
            raise FixtureSchemaError(
                f"{path}: order #{idx} 'symbol' must be a string, got "
                f"{type(order.get('symbol')).__name__}. Flatten nested QC symbol "
                "objects in the capture runbook."
            )
        symbol = order["symbol"].split(" ", 1)[0]  # strip QC security-id suffix
        order_type_code = int(order.get("type", 0))
        for ev_idx, event in enumerate(order["events"]):
            fill_qty = int(event["fillQuantity"])
            if fill_qty == 0:
                continue
            side: Side = "buy" if fill_qty > 0 else "sell"
            raw_time = event["time"]
            if isinstance(raw_time, str):
                event_dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                fill_time_ms = int(event_dt.timestamp() * 1000)
            elif isinstance(raw_time, (int, float)):
                # Disambiguate seconds vs ms: epoch-ms for 2001+ is > 1e12,
                # epoch-seconds < 1e11. Refuse anything in between as
                # potentially corrupted.
                raw_int = int(raw_time)
                if raw_int >= 10**12:
                    fill_time_ms = raw_int
                elif raw_int < 10**11:
                    fill_time_ms = raw_int * 1000
                else:
                    raise FixtureSchemaError(
                        f"{path}: order #{idx} event #{ev_idx} 'time' = {raw_int} is "
                        "ambiguous (neither clearly seconds nor ms). Normalize to "
                        "int64 ms UTC or ISO-8601 in the capture runbook."
                    )
            else:
                raise FixtureSchemaError(
                    f"{path}: order #{idx} event #{ev_idx} 'time' must be ISO string or "
                    f"numeric ms, got {type(raw_time).__name__}"
                )
            fee_raw = event.get("orderFeeAmount")
            fills.append(
                QcFill(
                    order_id=int(order["id"]),
                    symbol=symbol,
                    side=side,
                    fill_qty=fill_qty,
                    fill_price=Decimal(str(event["fillPrice"])),
                    fill_time_ms=fill_time_ms,
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
    """Check that each QC fill is explained by the captured bars.

    Resolution-aware:

    - **Daily fixture**: fill price must match the trading-date bar's
      ``open`` within ``fill_price_atol`` (canonical ``NEXT_BAR_OPEN``
      semantics — fill at next session's open).
    - **Minute fixture**: fill price must fall within ``[low, high]`` of
      the minute bar containing the fill's timestamp (QC's market-order
      fill happens at some price within that minute bar's range).

    Returns one ``FixtureAudit`` per unexplained fill (empty when the
    fixture is internally consistent). Audit failures route to
    ``FIXTURE_INSUFFICIENT`` and halt downstream classification.
    """
    if not qc_fills:
        return []
    symbol = qc_fills[0].symbol
    audits: list[FixtureAudit] = []
    if reader.is_minute_resolution:
        for qc in qc_fills:
            bar = reader.find_bar_containing(symbol, qc.fill_time_ms)
            if bar is None:
                audits.append(
                    FixtureAudit(
                        qc_fill=qc,
                        reason=(
                            f"no minute bar in fixture containing fill time "
                            f"{qc.fill_time_ms} ms (date {qc.trading_date})"
                        ),
                        expected_open=None,
                        actual_fill_price=qc.fill_price,
                    )
                )
                continue
            atol = tolerances.fill_price_atol
            if not (bar.low - atol <= qc.fill_price <= bar.high + atol):
                audits.append(
                    FixtureAudit(
                        qc_fill=qc,
                        reason=(
                            f"fill {qc.fill_price} outside minute-bar range "
                            f"[{bar.low}, {bar.high}] at {bar.time.isoformat()} "
                            f"(tolerance {atol})"
                        ),
                        expected_open=bar.open,
                        actual_fill_price=qc.fill_price,
                    )
                )
    else:
        opens = reader.bar_open_by_date(symbol)
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


def _enumerate_fills_by_key(
    fills: list[QcFill] | list[OurFill],
) -> dict[tuple[Side, Date, int], QcFill | OurFill]:
    """Index fills by ``(side, trading_date, within_day_sequence)``.

    Sequence is 0 for the first fill on that (date, side) and increments
    for each duplicate. Phase 3's single-symbol long-only spec produces
    at most one fill per (date, side), so any sequence > 0 indicates
    split orders / partial fills / same-day reversal — surfaced to the
    alignment step rather than collapsed.
    """
    sorted_fills = sorted(fills, key=lambda f: (f.trading_date, f.side, f.fill_time_ms))
    out: dict[tuple[Side, Date, int], QcFill | OurFill] = {}
    seq_by_key: dict[tuple[Side, Date], int] = {}
    for fill in sorted_fills:
        bucket = (fill.side, fill.trading_date)
        seq = seq_by_key.get(bucket, 0)
        out[(fill.side, fill.trading_date, seq)] = fill
        seq_by_key[bucket] = seq + 1
    return out


def _align_fills(
    qc_fills: list[QcFill],
    our_fills: list[OurFill],
) -> list[ReconciledPair]:
    """Pair QC fills with ours by ``(side, trading_date, within_day_seq)``.

    Daily AAPL long-only spec → expected sequence is always 0 (one fill per
    (date, side)). If sequence > 0 appears on either side, the surplus
    fills emerge as half-pairs that ``_classify_divergences`` reports as
    ``DECISION_MISMATCH`` — no fill is silently dropped.
    """
    qc_map = _enumerate_fills_by_key(qc_fills)
    ours_map = _enumerate_fills_by_key(our_fills)
    all_keys = sorted(set(qc_map) | set(ours_map), key=lambda x: (x[1], x[0], x[2]))
    pairs: list[ReconciledPair] = []
    for key in all_keys:
        qc_val = qc_map.get(key)
        ours_val = ours_map.get(key)
        # mypy/typeguard friendliness: each map is homogeneous by construction
        qc_fill = qc_val if isinstance(qc_val, QcFill) else None
        our_fill = ours_val if isinstance(ours_val, OurFill) else None
        pairs.append(
            ReconciledPair(
                qc=qc_fill,
                ours=our_fill,
                trading_date=key[1],
                side=key[0],
            )
        )
    return pairs


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


class RoundTripPairingError(ValueError):
    """Raised when a fill sequence cannot be paired into long-only round-trips.

    Phase 3 assumes single-position-at-a-time long-only trading. Two
    consecutive same-side fills, or an exit before any entry, indicate
    either a strategy-spec misuse or a fixture corruption — and either
    way, computing P&L on top of that data would mislead.
    """


def _pair_round_trips(
    fills: list[QcFill] | list[OurFill],
    tolerances: Tolerances,
) -> list[FillRoundTrip]:
    """Pair fills into entry/exit round-trips with computed realized P&L.

    Phase 3 invariant: long-only, one position at a time. Buy opens; next
    sell on a strictly later date closes; alternating thereafter. Anything
    else raises ``RoundTripPairingError`` so callers can surface it.

    Per-round-trip P&L tolerance is propagated from per-fill tolerances:
    ``(|entry_qty| + |exit_qty|) × per_share_pnl_atol + 2 × commission_atol``.
    """
    if not fills:
        return []
    sorted_fills = sorted(fills, key=lambda f: f.fill_time_ms)
    pending_entry: QcFill | OurFill | None = None
    out: list[FillRoundTrip] = []
    for fill in sorted_fills:
        if fill.side == "buy":
            if pending_entry is not None:
                raise RoundTripPairingError(
                    f"two consecutive buys at {pending_entry.trading_date} "
                    f"and {fill.trading_date} — Phase 3 long-only spec opens one "
                    "position at a time"
                )
            pending_entry = fill
        elif fill.side == "sell":
            if pending_entry is None:
                raise RoundTripPairingError(f"sell at {fill.trading_date} with no open position")
            entry_qty = pending_entry.fill_qty
            exit_qty = fill.fill_qty
            if abs(entry_qty) != abs(exit_qty):
                # Surface this so the reconciler can route it through the
                # alignment layer; pairing here would silently mask a
                # quantity mismatch into P&L drift.
                raise RoundTripPairingError(
                    f"entry qty {entry_qty} on {pending_entry.trading_date} "
                    f"does not match exit qty {exit_qty} on {fill.trading_date}"
                )
            shares = Decimal(abs(entry_qty))
            entry_fee = pending_entry.fee if pending_entry.fee is not None else Decimal("0")
            exit_fee = fill.fee if fill.fee is not None else Decimal("0")
            realized_pnl = (fill.fill_price - pending_entry.fill_price) * shares - entry_fee - exit_fee
            propagated_atol = (
                Decimal(abs(entry_qty)) + Decimal(abs(exit_qty))
            ) * tolerances.per_share_pnl_atol + Decimal(2) * tolerances.commission_atol
            out.append(
                FillRoundTrip(
                    entry_trading_date=pending_entry.trading_date,
                    entry_qty=entry_qty,
                    entry_price=pending_entry.fill_price,
                    entry_fee=entry_fee,
                    exit_trading_date=fill.trading_date,
                    exit_qty=exit_qty,
                    exit_price=fill.fill_price,
                    exit_fee=exit_fee,
                    realized_pnl=realized_pnl,
                    propagated_atol=propagated_atol,
                )
            )
            pending_entry = None
    return out


def _classify_pnl_divergences(
    qc_round_trips: list[FillRoundTrip],
    our_round_trips: list[FillRoundTrip],
) -> list[Divergence]:
    """Compare round-trip realized P&L between QC and ours.

    Round-trips are matched by ``entry_trading_date`` — Phase 3's single-
    position-at-a-time spec gives each date at most one round-trip. If a
    round-trip exists only on one side, ``DECISION_MISMATCH`` would have
    already fired in ``_classify_divergences`` for the missing entry or
    exit fill, so this helper only emits ``PNL_DRIFT`` for paired round-
    trips where realized P&L exceeds the propagated tolerance.

    Because the per-fill tolerances and the round-trip tolerance are
    derived from the same atols, fill+fee parity *algebraically implies*
    P&L parity by the triangle inequality. Gating on ``PNL_DRIFT`` is
    therefore redundant in the happy path — but emitting it makes the
    acceptance claim concrete rather than implicit and catches any
    aggregation or sign-attribution bug between fills and reported P&L.
    """
    qc_by_entry = {rt.entry_trading_date: rt for rt in qc_round_trips}
    our_by_entry = {rt.entry_trading_date: rt for rt in our_round_trips}
    out: list[Divergence] = []
    for entry_date in sorted(set(qc_by_entry) & set(our_by_entry)):
        qc_rt = qc_by_entry[entry_date]
        our_rt = our_by_entry[entry_date]
        diff = abs(qc_rt.realized_pnl - our_rt.realized_pnl)
        # Tolerances derive from the same per-fill atols, so we pick the
        # larger of the two pair tolerances (they're equal for matched-qty
        # round-trips, but be explicit).
        atol = max(qc_rt.propagated_atol, our_rt.propagated_atol)
        if diff > atol:
            out.append(
                Divergence(
                    category=DivergenceCategory.PNL_DRIFT,
                    pair=ReconciledPair(
                        qc=None,
                        ours=None,
                        trading_date=entry_date,
                        side="buy",
                    ),
                    detail=(
                        f"round-trip entry {entry_date}: qc_pnl={qc_rt.realized_pnl} "
                        f"our_pnl={our_rt.realized_pnl} diff={diff} > atol={atol}"
                    ),
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
    qc_round_trips: list[FillRoundTrip] = []
    our_round_trips: list[FillRoundTrip] = []
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
        # Round-trip P&L parity. We pair each side's fills into long-only
        # round-trips and compare realized P&L per entry date. Pairing
        # failures (consecutive same-side fills, qty mismatch, etc.) are
        # caught by ``_pair_round_trips`` and surfaced as PNL_DRIFT
        # divergences rather than allowed to silently mask P&L drift.
        try:
            qc_round_trips = _pair_round_trips(qc_fills, tolerances)
        except RoundTripPairingError as exc:
            divergences.append(
                Divergence(
                    category=DivergenceCategory.PNL_DRIFT,
                    pair=ReconciledPair(
                        qc=None,
                        ours=None,
                        trading_date=qc_fills[0].trading_date if qc_fills else Date.today(),
                        side=None,
                    ),
                    detail=f"QC round-trip pairing failed: {exc}",
                )
            )
        try:
            our_round_trips = _pair_round_trips(our_fills, tolerances)
        except RoundTripPairingError as exc:
            divergences.append(
                Divergence(
                    category=DivergenceCategory.PNL_DRIFT,
                    pair=ReconciledPair(
                        qc=None,
                        ours=None,
                        trading_date=our_fills[0].trading_date if our_fills else Date.today(),
                        side=None,
                    ),
                    detail=f"Our round-trip pairing failed: {exc}",
                )
            )
        if qc_round_trips and our_round_trips:
            divergences.extend(_classify_pnl_divergences(qc_round_trips, our_round_trips))

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
        qc_round_trips=qc_round_trips,
        our_round_trips=our_round_trips,
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
