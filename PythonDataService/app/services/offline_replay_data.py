"""Historical SPY/TSLA session preparation for offline bot replay."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from app.broker.ibkr.models import IbkrMinuteBar
from app.engine.data.availability import check_availability, ensure_range
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.policy_store import resolve_data_roots
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import (
    SessionWindow,
    session_window_for_date,
    session_windows_ms_utc,
)
from app.schemas.offline_replay import (
    OfflineReplayCatalogResponse,
    OfflineReplayCatalogSession,
    OfflineReplaySymbol,
)
from app.services.polygon_client import PolygonClientService

_ET = ZoneInfo("America/New_York")
_SOURCE_MINUTE_MS = 60_000


class OfflineReplayDataError(RuntimeError):
    """Historical data cannot satisfy the deterministic replay contract."""


class OfflineReplayDataUnavailableError(OfflineReplayDataError):
    """Required cached data is missing and auto-fetch is disabled."""


@dataclass(frozen=True, slots=True)
class PreparedOfflineReplay:
    session: SessionWindow
    warmup_minutes: int
    playback_minutes: int
    warmup_end_ms: int
    playback_end_ms: int
    bars_by_symbol: dict[OfflineReplaySymbol, tuple[IbkrMinuteBar, ...]]
    data_sha256_by_symbol: dict[OfflineReplaySymbol, str]


class OfflineReplayDataService:
    """Prepare immutable, aligned one-minute market streams."""

    def __init__(self, polygon: PolygonClientService | None = None) -> None:
        self._polygon = polygon or PolygonClientService()

    def catalog(
        self,
        *,
        now_ms: int,
        symbols: tuple[OfflineReplaySymbol, ...] = ("SPY", "TSLA"),
        warmup_minutes: int,
        playback_minutes: int,
        limit: int = 20,
    ) -> OfflineReplayCatalogResponse:
        """Return recent completed sessions, newest first."""
        now = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
        start = (now - timedelta(days=max(limit * 3, 45))).date()
        end = now.astimezone(_ET).date()
        roots = resolve_data_roots(source="polygon", adjusted=True)
        completed = [
            window
            for window in session_windows_ms_utc(start, end)
            if window.close_ms_utc < now_ms
        ][-limit:]

        sessions: list[OfflineReplayCatalogSession] = []
        required_minutes = warmup_minutes + playback_minutes
        for window in reversed(completed):
            duration_minutes = (window.close_ms_utc - window.open_ms_utc) // _SOURCE_MINUTE_MS
            cached_symbols = [
                symbol
                for symbol in symbols
                if check_availability(
                    roots,
                    symbol,
                    window.session_date,
                    window.session_date,
                    resolution="minute",
                ).is_complete
            ]
            sessions.append(
                OfflineReplayCatalogSession(
                    session_date_ms=window.open_ms_utc,
                    session_open_ms=window.open_ms_utc,
                    session_close_ms=window.close_ms_utc,
                    eligible=duration_minutes >= required_minutes,
                    cached_symbols=cached_symbols,
                )
            )

        recommended = next((item.session_date_ms for item in sessions if item.eligible), None)
        return OfflineReplayCatalogResponse(
            sessions=sessions,
            recommended_session_date_ms=recommended,
        )

    def prepare(
        self,
        *,
        session_date_ms: int,
        symbols: tuple[OfflineReplaySymbol, ...],
        warmup_minutes: int,
        playback_minutes: int,
        auto_fetch: bool,
        fetched_at_ms: int,
    ) -> PreparedOfflineReplay:
        """Materialize, validate, align, and fingerprint a replay window."""
        session_date = datetime.fromtimestamp(session_date_ms / 1000, tz=UTC).astimezone(_ET).date()
        session = session_window_for_date(session_date)
        if session.open_ms_utc != session_date_ms:
            raise OfflineReplayDataError(
                "session_date_ms must equal the canonical NYSE session-open timestamp"
            )

        required_minutes = warmup_minutes + playback_minutes
        session_minutes = (session.close_ms_utc - session.open_ms_utc) // _SOURCE_MINUTE_MS
        if required_minutes > session_minutes:
            raise OfflineReplayDataError(
                f"replay requires {required_minutes} source minutes but the selected "
                f"session contains only {session_minutes}"
            )

        roots = resolve_data_roots(source="polygon", adjusted=True)
        reference_roots = roots[:-1]
        cache_root = roots[-1]
        reader = LeanMinuteDataReader(roots, session="regular")
        prepared: dict[OfflineReplaySymbol, tuple[IbkrMinuteBar, ...]] = {}
        hashes: dict[OfflineReplaySymbol, str] = {}

        for symbol in symbols:
            coverage = check_availability(
                roots,
                symbol,
                session_date,
                session_date,
                resolution="minute",
            )
            if not coverage.is_complete:
                if not auto_fetch:
                    raise OfflineReplayDataUnavailableError(
                        f"{symbol} minute data is not cached for session_date_ms={session_date_ms}"
                    )
                coverage = ensure_range(
                    reference_roots=reference_roots,
                    cache_root=cache_root,
                    symbol=symbol,
                    start=session_date,
                    end=session_date,
                    polygon=self._polygon,
                    adjusted=True,
                    resolution="minute",
                )
            if not coverage.is_complete:
                raise OfflineReplayDataUnavailableError(
                    f"{symbol} data remained incomplete after materialization"
                )

            source_bars = reader.read_day(symbol, session_date)
            self._validate_source_bars(
                symbol=symbol,
                session=session,
                bars=source_bars,
                required_minutes=required_minutes,
            )
            selected = source_bars[:required_minutes]
            native = tuple(
                self._to_native_bar(bar, fetched_at_ms=fetched_at_ms)
                for bar in selected
            )
            prepared[symbol] = native
            hashes[symbol] = self._bar_digest(native)

        self._validate_symbol_alignment(prepared)
        first_symbol = symbols[0]
        first_stream = prepared[first_symbol]
        warmup_end_ms = (
            session.open_ms_utc
            if warmup_minutes == 0
            else first_stream[warmup_minutes - 1].end_ms
        )
        return PreparedOfflineReplay(
            session=session,
            warmup_minutes=warmup_minutes,
            playback_minutes=playback_minutes,
            warmup_end_ms=warmup_end_ms,
            playback_end_ms=first_stream[-1].end_ms,
            bars_by_symbol=prepared,
            data_sha256_by_symbol=hashes,
        )

    @staticmethod
    def _validate_source_bars(
        *,
        symbol: OfflineReplaySymbol,
        session: SessionWindow,
        bars: list[TradeBar],
        required_minutes: int,
    ) -> None:
        if len(bars) < required_minutes:
            raise OfflineReplayDataError(
                f"{symbol} has {len(bars)} RTH minute bars; {required_minutes} are required"
            )
        # Only the immutable replay prefix is in scope. A vendor gap later in
        # the session must not invalidate an already-complete requested window.
        starts = [
            int(bar.time.timestamp() * 1000)
            for bar in bars[:required_minutes]
        ]
        if starts[0] != session.open_ms_utc:
            raise OfflineReplayDataError(
                f"{symbol} first bar starts at {starts[0]}, expected {session.open_ms_utc}"
            )
        for previous, current in pairwise(starts):
            if current <= previous:
                raise OfflineReplayDataError(
                    f"{symbol} contains duplicate or non-monotonic timestamps: "
                    f"{previous} then {current}"
                )
            if current - previous != _SOURCE_MINUTE_MS:
                raise OfflineReplayDataError(
                    f"{symbol} contains a missing source minute between {previous} and {current}"
                )

    @staticmethod
    def _validate_symbol_alignment(
        bars_by_symbol: dict[OfflineReplaySymbol, tuple[IbkrMinuteBar, ...]],
    ) -> None:
        expected: tuple[int, ...] | None = None
        expected_symbol: str | None = None
        for symbol, bars in bars_by_symbol.items():
            timestamps = tuple(bar.start_ms for bar in bars)
            if expected is None:
                expected = timestamps
                expected_symbol = symbol
                continue
            if timestamps != expected:
                raise OfflineReplayDataError(
                    f"{symbol} timestamps do not align exactly with {expected_symbol}"
                )

    @staticmethod
    def _to_native_bar(bar: TradeBar, *, fetched_at_ms: int) -> IbkrMinuteBar:
        return IbkrMinuteBar(
            symbol=bar.symbol,
            start_ms=int(bar.time.timestamp() * 1000),
            end_ms=int(bar.end_time.timestamp() * 1000),
            open=Decimal(bar.open),
            high=Decimal(bar.high),
            low=Decimal(bar.low),
            close=Decimal(bar.close),
            volume=bar.volume,
            fetched_at_ms=fetched_at_ms,
            source="polygon",
            provenance="polygon_historical",
            venue="POLYGON",
            session_phase="RTH",
            use_rth=True,
        )

    @staticmethod
    def _bar_digest(bars: tuple[IbkrMinuteBar, ...]) -> str:
        digest = hashlib.sha256()
        for bar in bars:
            record = "|".join(
                (
                    bar.symbol,
                    str(bar.start_ms),
                    str(bar.end_ms),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    bar.provenance,
                )
            )
            digest.update(record.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()
