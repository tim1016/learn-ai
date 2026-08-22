"""Durable, unfiltered source bars for one account authority.

This ledger is intentionally separate from chart retention.  It captures the
exact closed feed observation before a strategy session consumes it, so a
restart can replay inputs without asking a provider to reconstruct history.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.marketdata.feed import MarketDataBar
from app.services.jsonl_wal import JsonlWal

SOURCE_BAR_LEDGER_FILENAME = "source_bars.jsonl"


class RetainedSourceBar(BaseModel):
    """One exact, closed feed observation with stable authority-scoped identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    account_id: str
    provider: str
    symbol: str
    bar_identity: str
    bar_ref: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = Field(ge=0)
    fetched_at_ms: int = Field(ge=0)
    session_phase: str

    @classmethod
    def from_market_bar(
        cls,
        *,
        seq: int,
        account_id: str,
        bar: MarketDataBar,
    ) -> RetainedSourceBar:
        provider = bar.feed_id
        identity = f"{provider}:{bar.symbol}:{bar.start_ms}:{bar.end_ms}"
        return cls(
            seq=seq,
            account_id=account_id,
            provider=provider,
            symbol=bar.symbol,
            bar_identity=identity,
            bar_ref=f"source-bar:{account_id}:{identity}",
            start_ms=bar.start_ms,
            end_ms=bar.end_ms,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            fetched_at_ms=bar.fetched_at_ms,
            session_phase=bar.session_phase,
        )


class SourceBarConflictError(RuntimeError):
    """A provider reused a stable bar identity with different closed payload."""


def _corrupt_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Source-bar ledger corrupt at {path}: {detail}")


class SourceBarLedger:
    """Fsync'd authority-scoped source-bar store with conflict-visible deduplication."""

    def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
        safe_account_id = safe_path_component(account_id, "account id")
        account_dir = resolve_contained_path(
            Path(artifacts_root), "accounts", "alpaca", safe_account_id
        )
        self.account_id = account_id
        self._wal: JsonlWal[RetainedSourceBar] = JsonlWal(
            account_dir / SOURCE_BAR_LEDGER_FILENAME,
            record_model=RetainedSourceBar,
            corrupt_error=_corrupt_error,
            seq_of=lambda row: row.seq,
            label="source_bar",
            trusted_root=account_dir,
        )
        self._lock = threading.Lock()
        self._by_identity: dict[str, RetainedSourceBar] = {}
        rows = self._wal.read_all()
        for row in rows:
            if row.bar_identity in self._by_identity:
                raise _corrupt_error(self.path, f"duplicate bar identity {row.bar_identity!r}")
            self._by_identity[row.bar_identity] = row
        # The initial identity scan also establishes the next durable sequence;
        # avoid a second full replay on the first append after construction.
        self._wal._next_seq = rows[-1].seq + 1 if rows else 1

    @property
    def path(self) -> Path:
        return self._wal.path

    def append(self, bar: MarketDataBar) -> RetainedSourceBar:
        """Append one exact unfiltered closed bar, or return its exact replay row.

        An exact feed redelivery is idempotent.  A same provider/symbol/time
        identity with a different payload is a revision and is refused rather
        than silently overwriting replay evidence.
        """
        with self._lock:
            candidate = RetainedSourceBar.from_market_bar(
                seq=self._wal.allocate_seq(), account_id=self.account_id, bar=bar
            )
            existing = self._by_identity.get(candidate.bar_identity)
            if existing is not None:
                # Fetch time is observation metadata, not the closed market
                # payload. A redelivery with the same bar fetched later must
                # replay the first durable observation rather than look like
                # a provider revision.
                if existing.model_dump(exclude={"seq", "fetched_at_ms"}) == candidate.model_dump(
                    exclude={"seq", "fetched_at_ms"}
                ):
                    return existing
                raise SourceBarConflictError(
                    "SOURCE_BAR_IDENTITY_CONFLICT: "
                    f"{candidate.bar_identity!r} was observed with a different payload"
                )
            self._wal.append(candidate)
            self._by_identity[candidate.bar_identity] = candidate
            return candidate

    def by_identity(self, bar_identity: str) -> RetainedSourceBar | None:
        """Return the exact durable observation for one stable source-bar identity."""
        with self._lock:
            cached = self._by_identity.get(bar_identity)
            if cached is not None:
                return cached
            # The synthetic broker and strategy runner intentionally open
            # separate handles to the same append-only authority. Refresh a
            # cache miss from the durable WAL so a just-appended decision bar
            # is visible across those handles without choosing a newer bar.
            for row in self._wal.read_all():
                existing = self._by_identity.get(row.bar_identity)
                if existing is not None and existing != row:
                    raise _corrupt_error(
                        self.path,
                        f"duplicate bar identity {row.bar_identity!r}",
                    )
                self._by_identity[row.bar_identity] = row
            return self._by_identity.get(bar_identity)

    def find_by_market_bar(self, bar: MarketDataBar) -> RetainedSourceBar | None:
        """Find the exact retained observation corresponding to one evaluated bar."""
        identity = f"{bar.feed_id}:{bar.symbol}:{bar.start_ms}:{bar.end_ms}"
        return self.by_identity(identity)

    def find_by_closed_end(
        self,
        *,
        provider: str,
        symbol: str,
        end_ms: int,
    ) -> RetainedSourceBar | None:
        """Return the one retained source observation for a decision close.

        A signal program may emit a consolidated decision only when the next
        raw bar arrives. Its ``bar_close_ms`` nevertheless names the closed
        raw observation from which a synthetic fill must be derived. More than
        one match is ambiguous evidence, so callers fail closed rather than
        choose by arrival order.
        """
        matches = [
            row
            for row in self.bars(provider=provider, symbol=symbol)
            if row.end_ms == end_ms
        ]
        if len(matches) > 1:
            raise SourceBarConflictError(
                "SOURCE_BAR_DECISION_AMBIGUITY: "
                f"{provider}:{symbol}:{end_ms} has {len(matches)} retained observations"
            )
        return matches[0] if matches else None

    def bars(self, *, provider: str, symbol: str) -> list[RetainedSourceBar]:
        """Return one provider/symbol's retained observations in durable order."""
        return [
            row
            for row in self._wal.read_all()
            if row.provider == provider and row.symbol == symbol
        ]

    def latest(self, *, provider: str, symbol: str) -> RetainedSourceBar | None:
        rows = self.bars(provider=provider, symbol=symbol)
        return rows[-1] if rows else None

    def latest_for_symbol(self, symbol: str) -> RetainedSourceBar | None:
        """Return the last retained source observation for one symbol.

        A sealed program binds its feed identity before this ledger is used.
        The synthetic port therefore reads the most recent *retained* bar,
        never another provider or a live price service.
        """
        rows = [row for row in self._wal.read_all() if row.symbol == symbol]
        return rows[-1] if rows else None


__all__ = [
    "SOURCE_BAR_LEDGER_FILENAME",
    "RetainedSourceBar",
    "SourceBarConflictError",
    "SourceBarLedger",
]
