"""Durable synthesized-order ledger for the authorities that never submit.

Two worlds write synthesized fills — the ``sim:`` Dry Run world and the
``shadow:`` world (ADR 0059 D2) — and both need the same core: an append-only,
cross-process-locked order WAL keyed by ``client_order_id``; a per-order binding
of the exact retained decision bar the Clerk evaluated; a verifier that the
bound bar is the durable observation and belongs to this authority; and the
average-cost position projection over the ledger's fills. What differs between
the worlds — how a leg becomes a fill, and where account truth comes from —
lives in ``synthetic_broker.py`` and ``shadow_broker.py``.

Every record may carry the ``leg`` it answered and a ``SynthesizedAnchor``: the
fill model and the decision bar the fill was synthesized from (ADR 0002's third
invariant — synthetic fills declare their provenance). Both are optional so
rows written before this module existed still parse.

Math Provenance Contract
------------------------
Formula: for each symbol, the projected position is an average-cost fold of
durable synthesized fills. Same-direction fills add signed entry notional;
reductions retain the prior average cost for the remaining quantity; a flip
opens only the residual at the flip fill price. A position is emitted iff
``position_quantity_is_nonzero(quantity)``.
Reference: average-cost broker position convention, recorded in
``docs/references/synthetic-broker-position-projection.md``.
Canonical implementation: ``project_positions`` in this module.
Validated against: ``tests/services/test_source_bar_ledger.py`` exact
buy/reduce/add/flip parity fixture (``atol=0``, ``rtol=0``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.services.jsonl_wal import JsonlWal
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.advisory_lock import advisory_file_lock

# The sim world's existing file name; the shadow world writes the same shape
# in its own custody directory, so one reader serves both.
SYNTHESIZED_ORDER_LEDGER_FILENAME = "simulated_orders.jsonl"
FillModel = Literal["decision_bar_close", "limit_touch"]
_ORDER_LOCKS: dict[str, threading.Lock] = {}
_ORDER_LOCKS_GUARD = threading.Lock()


class SynthesizedBarBindingError(RuntimeError):
    """A proposed synthesized fill is not bound to its exact retained source bar."""


class SynthesizedAnchor(BaseModel):
    """Where one synthesized order's fill came from (ADR 0002 invariant 3).

    ``cancel_at_ms`` is set only for a resting model — the instant the vendor
    would have cancelled the order. ``fill_bar_ref`` names the retained bar
    that produced the fill once one has; ``None`` while the order rests or
    after it cancelled unfilled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_model: FillModel
    evidence_account_id: str
    provider: str
    bar_identity: str
    bar_ref: str
    decision_bar_start_ms: int = Field(ge=0)
    decision_bar_end_ms: int = Field(ge=0)
    cancel_at_ms: int | None = Field(default=None, ge=0)
    fill_bar_ref: str | None = None


class SynthesizedOrderRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    order: BrokerOrder
    leg: BrokerOrderLeg | None = None
    anchor: SynthesizedAnchor | None = None


def _corrupt_order_ledger(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Synthetic order ledger corrupt at {path}: {detail}")


def _order_lock(path: Path) -> threading.Lock:
    """Return the process-local half of one order-ledger transaction lock."""
    key = str(path)
    with _ORDER_LOCKS_GUARD:
        lock = _ORDER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ORDER_LOCKS[key] = lock
        return lock


BarVerifier = Callable[[RetainedSourceBar], RetainedSourceBar]


def single_ledger_verifier(account_id: str, source_bars: SourceBarLedger) -> BarVerifier:
    """The sim world's verifier: one authority, one evidence ledger."""

    def verify(retained_bar: RetainedSourceBar) -> RetainedSourceBar:
        if retained_bar.account_id != account_id:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding belongs to a different account authority."
            )
        persisted = source_bars.by_identity(retained_bar.bar_identity)
        if persisted != retained_bar:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding is not the exact retained source-bar observation."
            )
        return persisted

    return verify


class SynthesizedOrderLedger:
    """Append-only order WAL plus exact decision-bar binding for one authority."""

    def __init__(
        self,
        *,
        account_id: str,
        path: Path,
        trusted_root: Path,
        verify_bar: BarVerifier,
        label: str,
    ) -> None:
        self.account_id = account_id
        self._verify_bar = verify_bar
        self._bound_bars: dict[str, RetainedSourceBar] = {}
        self._binding_lock = threading.Lock()
        self._orders: JsonlWal[SynthesizedOrderRecord] = JsonlWal(
            path,
            record_model=SynthesizedOrderRecord,
            corrupt_error=_corrupt_order_ledger,
            seq_of=lambda row: row.seq,
            label=label,
            trusted_root=trusted_root,
        )

    @classmethod
    def beside_source_bars(
        cls, *, account_id: str, source_bars: SourceBarLedger, label: str
    ) -> SynthesizedOrderLedger:
        """The sim world's layout: the WAL beside the authority's one evidence ledger."""
        if source_bars.account_id != account_id:
            raise SynthesizedBarBindingError(
                "Synthesized order ledger and retained source-bar ledger must share one account authority."
            )
        return cls(
            account_id=account_id,
            path=source_bars.path.with_name(SYNTHESIZED_ORDER_LEDGER_FILENAME),
            trusted_root=source_bars.path.parent,
            verify_bar=single_ledger_verifier(account_id, source_bars),
            label=label,
        )

    @property
    def path(self) -> Path:
        return self._orders.path

    # ── decision-bar binding ────────────────────────────────────────────

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        """Bind one minted Clerk order identity to one exact retained decision bar.

        The binding is consumed by that exact ``client_order_id`` on submit.
        Rebinding the same id deterministically replaces an unconsumed binding,
        while a later retained bar for the same symbol cannot replace it.
        """
        if not client_order_id:
            raise SynthesizedBarBindingError("Synthetic bar binding requires a client order id.")
        canonical = self.verified_retained_bar(retained_bar, symbol=None)
        with self._binding_lock:
            self._bound_bars[client_order_id] = canonical

    def consume_bound_bar(self, client_order_id: str) -> RetainedSourceBar | None:
        with self._binding_lock:
            return self._bound_bars.pop(client_order_id, None)

    def verified_retained_bar(
        self,
        retained_bar: RetainedSourceBar,
        *,
        symbol: str | None,
    ) -> RetainedSourceBar:
        if symbol is not None and retained_bar.symbol != symbol:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding does not match the submitted order symbol."
            )
        return self._verify_bar(retained_bar)

    # ── the order WAL ───────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[list[SynthesizedOrderRecord]]:
        """Serialize a full order-ledger read/check/append across threads and processes."""
        with _order_lock(self._orders.path), advisory_file_lock(self._orders.path):
            yield self._orders.read_all()

    def latest_orders(self) -> list[BrokerOrder]:
        return self.latest_orders_from_records(self._orders.read_all())

    def latest_records(self) -> dict[str, SynthesizedOrderRecord]:
        return self.latest_records_from(self._orders.read_all())

    @staticmethod
    def latest_records_from(
        records: list[SynthesizedOrderRecord],
    ) -> dict[str, SynthesizedOrderRecord]:
        latest: dict[str, SynthesizedOrderRecord] = {}
        for row in records:
            latest[row.order.client_order_id or row.order.order_id] = row
        return latest

    @classmethod
    def latest_orders_from_records(cls, records: list[SynthesizedOrderRecord]) -> list[BrokerOrder]:
        return [row.order for row in cls.latest_records_from(records).values()]

    def find_order(
        self,
        records: list[SynthesizedOrderRecord],
        client_order_id: str,
    ) -> BrokerOrder | None:
        record = self.find_record(records, client_order_id)
        return None if record is None else record.order

    def find_record(
        self,
        records: list[SynthesizedOrderRecord],
        client_order_id: str,
    ) -> SynthesizedOrderRecord | None:
        return next(
            (
                record
                for record in self.latest_records_from(records).values()
                if record.order.client_order_id == client_order_id
            ),
            None,
        )

    def append_locked(
        self,
        records: list[SynthesizedOrderRecord],
        *,
        order: BrokerOrder,
        leg: BrokerOrderLeg | None = None,
        anchor: SynthesizedAnchor | None = None,
    ) -> SynthesizedOrderRecord:
        """Append inside :meth:`transaction`; ``records`` is that transaction's read."""
        next_seq = records[-1].seq + 1 if records else 1
        # A sibling instance may have appended since this instance's previous
        # write. Refresh the WAL's sequence cache while holding the
        # cross-process transaction lock so it cannot reuse a sequence.
        self._orders._next_seq = next_seq
        record = SynthesizedOrderRecord(seq=next_seq, order=order, leg=leg, anchor=anchor)
        self._orders.append(record)
        records.append(record)
        return record


def project_positions(orders: list[BrokerOrder]) -> dict[str, tuple[float, float]]:
    """Fold fills into canonical average-cost ``(quantity, signed_notional)``.

    The result intentionally contains signed notional: a long's notional is
    positive and a short's is negative. That representation makes both the
    same-direction weighted average and a side-flip's residual opening price
    exact at the broker model's float boundary.
    """
    positions: dict[str, tuple[float, float]] = {}
    for order in orders:
        if order.filled_quantity <= 0 or order.filled_avg_price is None:
            continue
        signed_fill = order.filled_quantity if order.side.lower() == "buy" else -order.filled_quantity
        quantity, notional = positions.get(order.symbol, (0.0, 0.0))
        if not position_quantity_is_nonzero(quantity) or quantity * signed_fill > 0:
            positions[order.symbol] = (
                quantity + signed_fill,
                notional + signed_fill * order.filled_avg_price,
            )
            continue

        next_quantity = quantity + signed_fill
        if not position_quantity_is_nonzero(next_quantity):
            positions[order.symbol] = (0.0, 0.0)
            continue
        if quantity * next_quantity > 0:
            average_entry_price = abs(notional / quantity)
            positions[order.symbol] = (next_quantity, next_quantity * average_entry_price)
            continue
        positions[order.symbol] = (next_quantity, next_quantity * order.filled_avg_price)
    return positions


__all__ = [
    "SYNTHESIZED_ORDER_LEDGER_FILENAME",
    "BarVerifier",
    "FillModel",
    "SynthesizedAnchor",
    "SynthesizedBarBindingError",
    "SynthesizedOrderLedger",
    "SynthesizedOrderRecord",
    "project_positions",
    "single_ledger_verifier",
]
