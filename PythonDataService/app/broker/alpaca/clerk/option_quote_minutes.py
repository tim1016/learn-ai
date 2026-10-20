"""Append-only, indexed held-quote observations beside the existing pricing store (#3368)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.alpaca.paths import resolve_contained_path
from app.options.quotes import HeldQuote, receipt_times

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuote

_SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_quotes (
    symbol TEXT NOT NULL,
    minute_ms INTEGER NOT NULL,
    kept_at_ms INTEGER NOT NULL,
    quote_json TEXT NOT NULL,
    UNIQUE(symbol, minute_ms)
);
CREATE INDEX IF NOT EXISTS minute_quotes_window ON minute_quotes(minute_ms);
"""


class OptionQuoteMinutes:
    """Retain all observations without reparsing or compacting their whole history."""

    def __init__(self, path: Path, *, trusted_root: Path) -> None:
        self.path = resolve_contained_path(trusted_root, str(path.relative_to(trusted_root)))

    def keep(self, quotes: Mapping[str, HeldQuote], *, minute_ms: int, observed_at_ms: int) -> None:
        from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuote

        rows = [KeptOptionQuote(seq=1, symbol=symbol, quote=RecordedQuote.of(quote),
                                kept_at_ms=observed_at_ms, minute_ms=minute_ms)
                for symbol, quote in quotes.items() if receipt_times(quote)]
        if not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(_SCHEMA)
            with connection:
                connection.executemany(
                    "INSERT OR IGNORE INTO minute_quotes(symbol, minute_ms, kept_at_ms, quote_json) VALUES (?, ?, ?, ?)",
                    [(row.symbol, row.minute_ms, row.kept_at_ms, row.quote.model_dump_json()) for row in rows],
                )

    def read(self, *, from_ms: int, to_ms: int) -> list[KeptOptionQuote]:
        from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuote

        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT rowid, symbol, minute_ms, kept_at_ms, quote_json FROM minute_quotes "
                "WHERE minute_ms >= ? AND minute_ms < ? AND kept_at_ms < ? ORDER BY kept_at_ms, rowid",
                (from_ms, to_ms, to_ms),
            ).fetchall()
        return [KeptOptionQuote(seq=sequence, symbol=symbol, minute_ms=minute, kept_at_ms=observed,
                               quote=RecordedQuote.model_validate_json(quote))
                for sequence, symbol, minute, observed, quote in rows]
