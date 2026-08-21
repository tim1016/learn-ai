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
            for existing in self._wal.read_all():
                if existing.bar_identity != candidate.bar_identity:
                    continue
                if existing.model_dump(exclude={"seq"}) == candidate.model_dump(exclude={"seq"}):
                    return existing
                raise SourceBarConflictError(
                    "SOURCE_BAR_IDENTITY_CONFLICT: "
                    f"{candidate.bar_identity!r} was observed with a different payload"
                )
            self._wal.append(candidate)
            return candidate

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
