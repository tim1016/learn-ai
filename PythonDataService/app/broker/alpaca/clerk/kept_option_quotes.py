"""The option quotes a simulated account was handed, kept so a later fill or mark can be priced from them.

A Dry Run has no option market of its own. What it knows of a contract's
price is the quotes its Clerk was handed: with an entry, with an exit
decision, and whenever the runner passes on the newest one it holds for a
contract the bot holds. Each is appended here, beside the account's
simulated orders, with both sides' receipt times as they arrived.

Two questions are asked of what is kept, and they are not the same:

- **Which is the newest quote?** By when its sides were received. A simulated
  close with no usable quote of its own, and the run-end close, are priced
  from it, and carry its age.
- **What was last stated to be the newest held, and when?** By when it was
  handed over. The simulated account marks an open contract from it. A quiet
  contract's bid can stand unchanged for minutes: its receipt time grows old
  while it is still the bid, and each hand-over says so again.

So a hand-over is written every time, even of a quote already kept
(``keep``). A quote the Clerk hands back from its own durable facts, in case
this file was lost, says nothing of what is current: it is written once, as
of its own receipt (``restore``).

Nothing here judges a quote. Whether one is usable for a decision is
``app.options.quotes.quote_fault``'s to say, and how a fill is priced from
one is ``app.options.simulated_fill``'s.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.options.quotes import HeldQuote, receipt_times
from app.services.jsonl_wal import JsonlWal
from app.utils.advisory_lock import advisory_file_lock
from app.utils.session_anchors import MAX_TIMESTAMP_MS

KEPT_OPTION_QUOTES_FILENAME = "kept_option_quotes.jsonl"
_KEEP_LOCK = threading.Lock()


class KeptOptionQuote(BaseModel):
    """One quote as it was handed over, for one contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    # The contract's OCC symbol.
    symbol: str = Field(min_length=1)
    quote: RecordedQuote
    kept_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @property
    def received_ms(self) -> int:
        """When its newest side was received."""
        return max(receipt_times(self.quote.held))

    @property
    def ref(self) -> str:
        """What a record that used this quote cites."""
        return f"kept-option-quote:{self.symbol}:{self.seq}"


def _corrupt(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Kept option quotes corrupt at {path}: {detail}")


class KeptOptionQuotes:
    """Append-only store of the option quotes one simulated account was handed."""

    def __init__(self, *, path: Path, trusted_root: Path) -> None:
        self._wal: JsonlWal[KeptOptionQuote] = JsonlWal(
            path,
            record_model=KeptOptionQuote,
            corrupt_error=_corrupt,
            seq_of=lambda row: row.seq,
            label="kept_option_quote",
            trusted_root=trusted_root,
        )

    @classmethod
    def beside(cls, evidence_file: Path) -> KeptOptionQuotes:
        """The store that sits in the same account folder as ``evidence_file``."""
        return cls(path=evidence_file.with_name(KEPT_OPTION_QUOTES_FILENAME), trusted_root=evidence_file.parent)

    @classmethod
    def of_account(cls, *, artifacts_root: Path, account_id: str) -> KeptOptionQuotes:
        """One simulated account's store, in the folder its retained bars and simulated orders are in."""
        account_dir = resolve_contained_path(
            Path(artifacts_root), "accounts", "alpaca", safe_path_component(account_id, "account id")
        )
        return cls(path=account_dir / KEPT_OPTION_QUOTES_FILENAME, trusted_root=account_dir)

    def keep(self, symbol: str, quote: HeldQuote, *, kept_at_ms: int) -> None:
        """Keep the quote that is the newest held for a contract at ``kept_at_ms``.

        Written every time, so that a quote unchanged since it was last kept
        is stated again as the newest held. A quote with no side says
        nothing and is not written; nor is the same statement made twice.
        """
        self._append(symbol, quote, kept_at_ms=kept_at_ms, once=False)

    def restore(self, symbol: str, quote: HeldQuote) -> None:
        """Put back a quote the caller holds durably, if it is not kept: as of its own receipt, never as of now."""
        times = receipt_times(quote)
        if times:
            self._append(symbol, quote, kept_at_ms=max(times), once=True)

    def _append(self, symbol: str, quote: HeldQuote, *, kept_at_ms: int, once: bool) -> None:
        if not receipt_times(quote):
            return
        recorded = RecordedQuote.of(quote)
        with _KEEP_LOCK, advisory_file_lock(self._wal.path):
            rows = self._wal.read_all()
            if any(
                row.symbol == symbol and row.quote == recorded and (once or row.kept_at_ms == kept_at_ms)
                for row in rows
            ):
                return
            next_seq = rows[-1].seq + 1 if rows else 1
            # Another process may have written since this one last did.
            self._wal.reset_seq(next_seq)
            self._wal.append(KeptOptionQuote(seq=next_seq, symbol=symbol, quote=recorded, kept_at_ms=kept_at_ms))

    def newest(
        self,
        symbol: str,
        *,
        at_or_before_ms: int | None = None,
        with_side: Literal["bid", "ask"] | None = None,
    ) -> KeptOptionQuote | None:
        """The quote kept for ``symbol`` whose sides were received last.

        ``at_or_before_ms`` leaves out one received later than an instant.
        ``with_side`` leaves out one that side had not arrived for: a quote
        with an ask alone says nothing of what the contract could be sold at.
        """
        if not self._wal.path.exists():
            return None
        candidates = [
            row
            for row in self._wal.read_all()
            if row.symbol == symbol
            and (at_or_before_ms is None or row.received_ms <= at_or_before_ms)
            and (with_side is None or getattr(row.quote, with_side) is not None)
        ]
        return max(candidates, key=lambda row: (row.received_ms, row.seq), default=None)

    def last_stated(
        self,
        symbol: str,
        *,
        at_or_before_ms: int,
        with_side: Literal["bid", "ask"] | None = None,
    ) -> KeptOptionQuote | None:
        """The quote most lately handed over as the newest held for ``symbol``, no later than an instant."""
        if not self._wal.path.exists():
            return None
        candidates = [
            row
            for row in self._wal.read_all()
            if row.symbol == symbol
            and row.kept_at_ms <= at_or_before_ms
            and (with_side is None or getattr(row.quote, with_side) is not None)
        ]
        return max(candidates, key=lambda row: (row.kept_at_ms, row.seq), default=None)


__all__ = ["KEPT_OPTION_QUOTES_FILENAME", "KeptOptionQuote", "KeptOptionQuotes"]
