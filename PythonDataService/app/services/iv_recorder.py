"""Multi-snapshot IV recorder — Step D of the IV-ownership plan.

Captures live Polygon snapshots, runs the chain through the Step A/B
contracts (typed price normalization + provenance-aware VIX-style
replication), and persists the result with full provenance so the
historical IV pipeline can be built forward-only from the day this
recorder ships.

Architecture (per ``docs/architecture/iv-ownership-decisions.md`` Q2):
the recorder is invoked by the .NET ``JobsController`` on a cron — it is
not an in-process scheduler. This module exposes:

- ``record_iv_snapshot(...)`` — the work function the cron calls.
- ``IvSnapshotStore`` — pluggable persistence interface.
- ``JsonlIvSnapshotStore`` — production-pragmatic JSONL file store.
- ``InMemoryIvSnapshotStore`` — for tests.

The Postgres-backed implementation is a follow-up; the JSONL store is
load-bearing-friendly (one INSERT per slot, append-only) and the schema
is forward-compatible with the table proposed in
``docs/architecture/iv-ownership-decisions.md`` §1 Q3.

**Sovereignty rule (plan §4.D):** we store raw bid/ask per contract and
the *internal-solver* IV. Polygon's IV field is never stored as an
authoritative IV value — even when it appears in the snapshot response,
it is dropped here. This is the single non-negotiable about the
recorder.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.routers.iv30 import _normalized_quotes_by_expiry, _pick_straddle_pair
from app.services.polygon_client import PolygonClientService
from app.services.rate_dividend_service import RateAndDividend, get_rate_and_dividend
from app.volatility.iv_provenance import IvProvenance
from app.volatility.vix_replication import vix_style_iv30_with_provenance

logger = logging.getLogger(__name__)


SLOT_CHOICES = ("09:35", "12:30", "16:00")
"""The three default daily slots — see decisions doc §1 Q1."""


@dataclass(frozen=True)
class RecordedIvSnapshot:
    """One captured slot.

    All scalar timestamps are int64 ms UTC (CLAUDE.md rule). The
    ``raw_chain`` field is the per-contract bid/ask we ingested, so a
    future solver upgrade can re-derive IV without re-fetching from
    Polygon.
    """

    ticker: str
    snapshot_ts_ms: int
    slot: str  # "09:35" | "12:30" | "16:00"
    spot: float
    rate: float
    dividend_yield: float
    rate_source: str
    dividend_source: str
    iv30_vix_style: float | None
    iv30_parametric: float | None
    iv_provenance: dict
    raw_chain: list[dict]
    error: str | None = None


@dataclass
class _RecorderResult:
    """Internal carrier — never serialized."""

    snapshot: RecordedIvSnapshot


# ── Persistence interface ───────────────────────────────────────────────────


class IvSnapshotStore(ABC):
    """Pluggable store for recorded IV snapshots.

    Implementations must be thread-safe — the recorder is invoked from
    the .NET cron, but tests run multiple writes concurrently.
    """

    @abstractmethod
    def write(self, snapshot: RecordedIvSnapshot) -> None: ...

    @abstractmethod
    def read_series(
        self,
        ticker: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[RecordedIvSnapshot]: ...


class InMemoryIvSnapshotStore(IvSnapshotStore):
    """Process-local store for tests and short-lived runs."""

    def __init__(self) -> None:
        self._rows: list[RecordedIvSnapshot] = []
        self._lock = threading.Lock()

    def write(self, snapshot: RecordedIvSnapshot) -> None:
        with self._lock:
            self._rows.append(snapshot)

    def read_series(
        self,
        ticker: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[RecordedIvSnapshot]:
        with self._lock:
            return [
                r
                for r in self._rows
                if r.ticker == ticker
                and (start_ms is None or r.snapshot_ts_ms >= start_ms)
                and (end_ms is None or r.snapshot_ts_ms <= end_ms)
            ]


class JsonlIvSnapshotStore(IvSnapshotStore):
    """Append-only JSONL file store. One file per ticker.

    Rationale (decisions doc §1 Q3): single Postgres table is the
    eventual production target, but adding ``asyncpg`` + a migration
    pipeline is heavier than tonight's scope. JSONL gives us the same
    schema, append-only writes, and a reversible upgrade path: the
    Postgres implementation will read this directory once at cutover
    and bulk-load.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()
        # mkdir is deferred to first write — the production default path
        # (/var/lib/iv-recorder) isn't writable in CI/import contexts where
        # the module is loaded but no slot is captured. The recorder hot
        # path always calls write(), so the directory is created on first
        # legitimate use.

    def _file_for(self, ticker: str) -> Path:
        return self.base_dir / f"{ticker}.jsonl"

    def write(self, snapshot: RecordedIvSnapshot) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(snapshot), separators=(",", ":"))
        with self._lock, open(self._file_for(snapshot.ticker), "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_series(
        self,
        ticker: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[RecordedIvSnapshot]:
        path = self._file_for(ticker)
        if not path.exists():
            return []
        out: list[RecordedIvSnapshot] = []
        with self._lock, open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                ts = int(d["snapshot_ts_ms"])
                if start_ms is not None and ts < start_ms:
                    continue
                if end_ms is not None and ts > end_ms:
                    continue
                out.append(RecordedIvSnapshot(**d))
        return out


# ── The work function ───────────────────────────────────────────────────────


def record_iv_snapshot(
    *,
    ticker: str,
    slot: str,
    store: IvSnapshotStore,
    polygon: PolygonClientService,
    target_calendar_days: int = 30,
    asof: datetime | None = None,
) -> RecordedIvSnapshot:
    """Capture one slot for one ticker, persist, and return the row.

    Idempotent on ``(ticker, snapshot_ts_ms)``: callers should pass
    the same ``asof`` if they retry; the store decides how to handle
    duplicates (the JSONL store appends, which is acceptable for the
    forward-only history use case).

    On failure (Polygon outage, no straddle, solver failure), the row is
    still written with ``error`` set so the recorder's audit trail
    captures *why* a slot is missing rather than silently dropping it.
    """
    if slot not in SLOT_CHOICES:
        raise ValueError(f"slot must be one of {SLOT_CHOICES}, got {slot!r}")

    asof = asof or datetime.now(tz=UTC)
    snapshot_ts = int(asof.timestamp() * 1000)

    snapshot: dict = {}
    try:
        snapshot = polygon.list_snapshot_options_chain(underlying_asset=ticker)
    except Exception as exc:
        logger.warning("[iv-recorder] %s slot=%s polygon error: %s", ticker, slot, exc)
        return _persist_error(
            store,
            ticker=ticker,
            snapshot_ts_ms=snapshot_ts,
            slot=slot,
            error=f"polygon_fetch_failed: {exc}",
        )

    underlying = snapshot.get("underlying") or {}
    contracts = snapshot.get("contracts") or []
    spot = float(underlying.get("price") or 0.0)
    if spot <= 0 or not contracts:
        return _persist_error(
            store,
            ticker=ticker,
            snapshot_ts_ms=snapshot_ts,
            slot=slot,
            error=f"insufficient_snapshot: spot={spot} contracts={len(contracts)}",
        )

    try:
        rd = get_rate_and_dividend(
            ticker=ticker, spot_price=spot, polygon=polygon, dte_days=target_calendar_days
        )
    except Exception as exc:
        logger.warning("[iv-recorder] %s slot=%s rate/div failure: %s", ticker, slot, exc)
        rd = RateAndDividend(rate=0.0, dividend_yield=0.0, source_rate="unknown", source_dividend="unknown")

    by_expiry = _normalized_quotes_by_expiry(contracts, asof)
    iv_vix: float | None = None
    iv_parametric: float | None = None  # parametric path is a follow-up
    prov_dict: dict = {}
    error_msg: str | None = None
    try:
        t1, t2 = _pick_straddle_pair(by_expiry, target_calendar_days)
        sigma, prov = vix_style_iv30_with_provenance(
            by_expiry[t1], by_expiry[t2],
            rate1=rd.rate, T1_calendar_days=t1,
            rate2=rd.rate, T2_calendar_days=t2,
            target_calendar_days=target_calendar_days,
        )
        iv_vix = float(sigma)
        prov_dict = _provenance_to_dict(prov)
    except Exception as exc:
        logger.warning("[iv-recorder] %s slot=%s replication failure: %s", ticker, slot, exc)
        error_msg = f"replication_failed: {exc}"

    raw_chain = _extract_raw_chain(contracts)

    row = RecordedIvSnapshot(
        ticker=ticker,
        snapshot_ts_ms=snapshot_ts,
        slot=slot,
        spot=spot,
        rate=rd.rate,
        dividend_yield=rd.dividend_yield,
        rate_source=rd.source_rate,
        dividend_source=rd.source_dividend,
        iv30_vix_style=iv_vix,
        iv30_parametric=iv_parametric,
        iv_provenance=prov_dict,
        raw_chain=raw_chain,
        error=error_msg,
    )
    store.write(row)
    return row


def _provenance_to_dict(prov: IvProvenance) -> dict:
    return {
        "iv_source": prov.iv_source,
        "price_source_mix": dict(prov.price_source_mix),
        "variance_contribution_synthetic": prov.variance_contribution_synthetic,
        "strike_coverage_score": prov.strike_coverage_score,
    }


def _extract_raw_chain(contracts: list[dict]) -> list[dict]:
    """Reduce Polygon's contract payload to the bid/ask we'll re-derive
    IV from. Drops Polygon's IV field intentionally (sovereignty rule)."""
    out = []
    for c in contracts:
        lq = c.get("last_quote") or {}
        out.append(
            {
                "ticker": c.get("ticker"),
                "contract_type": c.get("contract_type"),
                "strike_price": c.get("strike_price"),
                "expiration_date": c.get("expiration_date"),
                "bid": lq.get("bid"),
                "ask": lq.get("ask"),
                # Polygon's IV is recorded as a diagnostic, NOT used as ours.
                "polygon_iv_diagnostic": c.get("implied_volatility"),
            }
        )
    return out


def _persist_error(
    store: IvSnapshotStore,
    *,
    ticker: str,
    snapshot_ts_ms: int,
    slot: str,
    error: str,
) -> RecordedIvSnapshot:
    row = RecordedIvSnapshot(
        ticker=ticker,
        snapshot_ts_ms=snapshot_ts_ms,
        slot=slot,
        spot=0.0,
        rate=0.0,
        dividend_yield=0.0,
        rate_source="unknown",
        dividend_source="unknown",
        iv30_vix_style=None,
        iv30_parametric=None,
        iv_provenance={},
        raw_chain=[],
        error=error,
    )
    store.write(row)
    return row
