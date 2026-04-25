"""Options companion CSV builder — per-slot layout.

Emits one CSV per ``(side, slot)`` pair under ``calls/`` and ``puts/`` ZIP
subfolders, where ``slot`` is a price-ordered offset from ATM (e.g.
``atm-03``, ``atm``, ``atm+02``). The contract filling each slot rolls daily
based on the prior trading day's close; the slot semantic is stable. See
``docs/options-companion-format.md`` for the full format spec.

IV is solved per bar via ``app.volatility.solver.implied_volatility``
(QuantLib primary, Brent fallback). Greeks are computed via QuantLib's
``AnalyticEuropeanEngine`` using the solved IV. Surface-based IV is
intentionally NOT used as input — it remains a deferred cross-check.
Per-bar Greek values are pending a formal parity pass against LEAN /
QuantLib's analytic engine; see ``docs/math-sources-of-truth.md``.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.models.requests import OptionsCompanionConfig
from app.services.polygon_client import PolygonClientService
from app.services.quantlib_pricer import _QL_AVAILABLE, PricingEngine, price_option
from app.volatility.solver import SolveStatus, implied_volatility

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_MS_PER_YEAR = 365.0 * 24 * 3600 * 1000


@dataclass(frozen=True)
class _SelectedContract:
    ticker: str
    strike: float
    expiration: date
    contract_type: str  # 'call' | 'put'
    slot_offset: int  # ATM-relative; -strikes_each_side..+strikes_each_side


def _slot_label(offset: int) -> str:
    """Render a slot offset as a sortable, file-safe label.

    Format: ``atm`` for offset 0, otherwise ``atm{sign}{|offset|:02d}`` —
    e.g. ``atm-03``, ``atm+02``. Two-digit zero-pad keeps within-sign
    lexical sort consistent up to offset 99 (max config caps at 25).
    """
    if offset == 0:
        return "atm"
    sign = "+" if offset > 0 else "-"
    return f"atm{sign}{abs(offset):02d}"


def _utc_ms_for_et_close(d: date, hour: int = 16, minute: int = 0) -> int:
    """Return the `int64 ms UTC` timestamp for a given ET wall-clock close on date `d`."""
    et_dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ET)
    return int(et_dt.timestamp() * 1000)


def _prior_day_close_map(bars_df: pd.DataFrame) -> dict[date, float]:
    """Build `{trading_day -> close_price}` from sorted underlying minute bars.

    Uses the last bar's close per ET trading date. Caller aligns prior-day
    close to target day by looking up `target_day - 1 trading day`.
    """
    if bars_df.empty:
        return {}

    dt_utc = pd.to_datetime(bars_df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    trading_date = dt_et.dt.date

    last_close = bars_df.groupby(trading_date)["close"].last()
    return {d: float(c) for d, c in last_close.items()}


_MS_PER_UNIT = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


def _bar_grid_floor_ms(ts_ms: int, timespan: str, multiplier: int) -> int:
    """Floor a timestamp to the start of its Polygon aggregate bar.

    Polygon emits bar-start timestamps on a UTC-anchored grid shared by stock
    and option aggregate endpoints (same ``/v2/aggs/ticker/{ticker}/range``
    path, same rules). In theory a direct int equality over the dict would
    suffice — this defensive floor protects the IV-solve from drifting if
    Polygon ever introduces a session-aligned variant.
    """
    unit_ms = _MS_PER_UNIT.get(timespan, 60_000)
    period_ms = unit_ms * max(multiplier, 1)
    return (ts_ms // period_ms) * period_ms


def _underlying_close_map(bars_df: pd.DataFrame, timespan: str, multiplier: int) -> dict[int, float]:
    """Build `{bar_start_ms -> underlying_close}` for option-bar alignment.

    Keys are floored to the bar grid so option lookups are robust regardless
    of micro-drift between the two series.
    """
    if bars_df.empty:
        return {}
    return {
        _bar_grid_floor_ms(int(ts), timespan, multiplier): float(c)
        for ts, c in zip(bars_df["timestamp"], bars_df["close"], strict=False)
    }


def _select_strikes_with_slots(
    contracts: list[dict[str, Any]],
    prior_close: float,
    strikes_each_side: int,
) -> list[tuple[int, dict[str, Any]]]:
    """Return ``[(slot_offset, contract), ...]`` for offsets that have a listed strike.

    The slot offset is ATM-relative: ``-strikes_each_side..+strikes_each_side``
    where 0 is the strike closest to ``prior_close``. Offsets that fall
    outside the listed chain (chain edge) are simply omitted — the
    corresponding slot file gets no row for that day.
    """
    if not contracts:
        return []

    sorted_c = sorted(contracts, key=lambda c: c["strike_price"])
    strikes = [c["strike_price"] for c in sorted_c]
    atm_idx = int(np.argmin(np.abs(np.asarray(strikes) - prior_close)))

    out: list[tuple[int, dict[str, Any]]] = []
    for offset in range(-strikes_each_side, strikes_each_side + 1):
        idx = atm_idx + offset
        if 0 <= idx < len(sorted_c):
            out.append((offset, sorted_c[idx]))
    return out


def _resolve_target_expiry(
    polygon: PolygonClientService,
    ticker: str,
    trading_day: date,
    config: OptionsCompanionConfig,
) -> date | None:
    """Return the expiry to use for ``trading_day``, or ``None`` to skip.

    Strict-DTE policy: target = ``trading_day + config.dte_distance``. The
    expiry is returned only if Polygon lists a chain on exactly that date.
    No tolerance — fuzzy matches would mix DTEs in the same row, which is
    not a meaningful comparison for time-decay analysis.
    """
    target = trading_day + timedelta(days=config.dte_distance)
    target_iso = target.isoformat()
    try:
        # ``expired=True`` is critical: Polygon's /v3/reference/options/contracts
        # filters out expired chains by default, so any historical trading day
        # would otherwise look "no chain listed" and get incorrectly skipped.
        expirations = polygon.list_options_expirations(
            underlying_ticker=ticker,
            expiration_date_gte=target_iso,
            expiration_date_lte=target_iso,
            expired=True,
        )
    except Exception as exc:
        logger.warning(f"[OC] Expiration lookup failed for {ticker} on {trading_day}: {exc}")
        return None

    if target_iso in expirations:
        return target
    return None


def _fetch_contracts_for_expiry(
    polygon: PolygonClientService,
    ticker: str,
    trading_day: date,
    expiry: date,
    contract_type: str,
) -> list[dict[str, Any]]:
    """List contracts for a single underlying/expiry/type, tolerant of failures."""
    try:
        return polygon.list_options_contracts(
            underlying_ticker=ticker,
            as_of_date=trading_day.isoformat(),
            contract_type=contract_type,
            expiration_date=expiry.isoformat(),
            expired=True,
            limit=500,
        )
    except Exception as exc:
        logger.warning(f"[OC] Contract list failed {ticker} {expiry} {contract_type}: {exc}")
        return []


def _compute_row_greeks(
    option_close: float,
    underlying_spot: float,
    strike: float,
    bar_ts_ms: int,
    expiry_close_ms: int,
    is_call: bool,
    config: OptionsCompanionConfig,
) -> dict[str, float | None]:
    """Solve IV then compute Greeks via QuantLib analytic engine. Skip values if inputs degenerate."""
    ttm_years = max(0.0, (expiry_close_ms - bar_ts_ms) / _MS_PER_YEAR)
    out: dict[str, float | None] = {
        "iv": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
    }

    if ttm_years <= 0 or option_close is None or option_close <= 0 or underlying_spot is None:
        return out

    iv_result = implied_volatility(
        option_price=option_close,
        spot=underlying_spot,
        strike=strike,
        ttm=ttm_years,
        rate=config.risk_free_rate,
        dividend=config.dividend_yield,
        is_call=is_call,
    )

    if iv_result.status in (SolveStatus.QUANTLIB_OK, SolveStatus.BRENT_FALLBACK, SolveStatus.OK) and iv_result.iv:
        out["iv"] = iv_result.iv
    else:
        return out

    if not _QL_AVAILABLE:
        return out

    try:
        expiry_d = datetime.utcfromtimestamp(expiry_close_ms / 1000).date()
        eval_d = datetime.utcfromtimestamp(bar_ts_ms / 1000).date()
        greeks = price_option(
            spot=underlying_spot,
            strike=strike,
            risk_free_rate=config.risk_free_rate,
            volatility=iv_result.iv,
            expiration_date=expiry_d,
            option_type="call" if is_call else "put",
            evaluation_date=eval_d,
            dividend_yield=config.dividend_yield,
            engine=PricingEngine.ANALYTIC_BS,
        )
        out["delta"] = greeks.delta
        out["gamma"] = greeks.gamma
        out["theta"] = greeks.theta
        out["vega"] = greeks.vega
        out["rho"] = greeks.rho
    except Exception as exc:
        logger.debug(f"[OC] Greeks compute failed at ts={bar_ts_ms}: {exc}")

    return out


def _columns_for(config: OptionsCompanionConfig) -> list[str]:
    """Return the ordered column list for a per-slot CSV.

    Side is encoded by the parent folder (``calls/`` or ``puts/``); slot is
    encoded by the filename (``atm-03.csv`` etc.) — neither appears as a
    column. Per-row identity columns (contract_ticker, strike, expiration)
    let the user reconstruct the contract that filled this slot at each bar.
    """
    cols: list[str] = ["unix_ts", "iso_time"]
    if config.include_discontinuity:
        cols.append("discontinuity")
    cols += ["contract_ticker", "strike", "expiration"]
    if config.include_ohlcv:
        cols += ["open", "high", "low", "close", "volume"]
    if config.include_vwap:
        cols.append("vwap")
    if config.include_transactions:
        cols.append("transactions")
    if config.include_open_interest:
        cols.append("open_interest")
    if config.include_iv:
        cols.append("iv")
    if config.include_delta:
        cols.append("delta")
    if config.include_gamma:
        cols.append("gamma")
    if config.include_theta:
        cols.append("theta")
    if config.include_vega:
        cols.append("vega")
    if config.include_rho:
        cols.append("rho")
    return cols


def _fmt(v: Any) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, float):
        return f"{v:.8f}"
    return str(v)


def _mark_discontinuity(rows: list[dict[str, Any]]) -> None:
    """Tag each row's ``discontinuity`` field by comparing contract_ticker
    against the prior row's. The first row is always 1 (start of series =
    new instrument). Mutates rows in place; assumes rows already sorted by
    ``unix_ts``.
    """
    prev_ticker: str | None = None
    for row in rows:
        cur = row.get("contract_ticker")
        row["discontinuity"] = 1 if cur != prev_ticker else 0
        prev_ticker = cur


def _write_rows_to_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    """Serialize rows to CSV bytes. Caller must have sorted by unix_ts and
    populated ``discontinuity`` (when included in ``columns``)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_fmt(row.get(c)) for c in columns])
    return buf.getvalue().encode("utf-8")


def _process_contract(
    contract: _SelectedContract,
    trading_day: date,
    to_date: str,
    underlying_by_ts: dict[int, float],
    config: OptionsCompanionConfig,
    timespan: str,
    multiplier: int,
    polygon: PolygonClientService,
) -> list[dict[str, Any]]:
    """Fetch the option's bars for ``trading_day`` and build one row per bar.

    Fetch window is clamped to ``[trading_day, min(expiration, to_date)]``.
    The contract only produces bars while live; for ``dte_distance=0`` the
    window collapses to a single trading date.
    """
    contract_end = min(contract.expiration.isoformat(), to_date)
    try:
        bars = polygon.fetch_aggregates(
            ticker=contract.ticker,
            multiplier=max(multiplier, 1),
            timespan=timespan,
            from_date=trading_day.isoformat(),
            to_date=contract_end,
            adjusted=True,
        )
    except Exception as exc:
        logger.warning(f"[OC] Bar fetch failed for {contract.ticker} on {trading_day}: {exc}")
        return []

    if not bars:
        return []

    expiry_close_ms = _utc_ms_for_et_close(contract.expiration, hour=16, minute=0)
    is_call = contract.contract_type == "call"
    iso_expiration = contract.expiration.isoformat()

    rows: list[dict[str, Any]] = []
    for bar in bars:
        ts = int(bar["timestamp"])
        iso_time = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
        aligned_key = _bar_grid_floor_ms(ts, timespan, multiplier)
        underlying_spot = underlying_by_ts.get(aligned_key)

        row: dict[str, Any] = {
            "unix_ts": ts,
            "iso_time": iso_time,
            "contract_ticker": contract.ticker,
            "expiration": iso_expiration,
            "strike": contract.strike,
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "volume": bar.get("volume"),
            "vwap": bar.get("vwap"),
            "transactions": bar.get("transactions"),
            "open_interest": None,  # historical per-minute OI unavailable from Polygon
        }

        needs_greeks = (
            config.include_iv
            or config.include_delta
            or config.include_gamma
            or config.include_theta
            or config.include_vega
            or config.include_rho
        )
        if needs_greeks and underlying_spot is not None:
            greeks = _compute_row_greeks(
                option_close=bar.get("close"),
                underlying_spot=underlying_spot,
                strike=contract.strike,
                bar_ts_ms=ts,
                expiry_close_ms=expiry_close_ms,
                is_call=is_call,
                config=config,
            )
            row.update(greeks)

        rows.append(row)

    return rows


def build_options_companion_csvs(
    underlying_bars_df: pd.DataFrame,
    ticker: str,
    from_date: str,
    to_date: str,
    config: OptionsCompanionConfig,
    polygon: PolygonClientService,
    timespan: str = "minute",
    multiplier: int = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Build per-slot CSV byte payloads plus a summary report.

    Returns
    -------
    (slot_files, report)
        ``slot_files`` is a dict mapping ZIP-relative paths
        (``"calls/atm-03.csv"``, ``"puts/atm.csv"`` …) to encoded bytes.
        Empty slots (no row produced) are omitted. ``report`` captures
        skipped days, chosen expiries, and contract counts for the
        metadata bundle.

    The expiry policy is strict — for each trading day, only the listed
    expiry exactly matching ``trading_day + config.dte_distance`` is used;
    days without a matching expiry are skipped entirely.
    """
    if not config.enabled or underlying_bars_df.empty:
        return {}, {"enabled": False, "reason": "disabled or empty bars"}

    # Lazy import to avoid a circular: options_companion_service is imported
    # from app.routers.dataset, which itself imports from dataset_service.
    from app.services.dataset_service import RunCancelledError

    prior_close_by_day = _prior_day_close_map(underlying_bars_df)
    underlying_by_ts = _underlying_close_map(underlying_bars_df, timespan, multiplier)

    trading_days = sorted(prior_close_by_day.keys())
    if not trading_days:
        return {}, {"enabled": True, "reason": "no trading days derived from bars"}

    # Seed prior-close for the first day in range — fall back to first bar's
    # close on that day if we don't have a prior trading day.
    if len(trading_days) >= 2:
        first_day_close = prior_close_by_day[trading_days[0]]
    else:
        first_day_close = float(underlying_bars_df["close"].iloc[0])

    # Slot rows accumulate per (side, slot_offset) and are written out at the
    # end after sorting and discontinuity tagging.
    slot_rows: dict[tuple[str, int], list[dict[str, Any]]] = {}
    skipped_days: list[dict[str, str]] = []
    per_day_report: list[dict[str, Any]] = []
    calls_done = 0
    puts_done = 0

    def _check_cancel() -> None:
        if cancel_check is not None and cancel_check():
            raise RunCancelledError(f"options companion cancelled after {calls_done} calls, {puts_done} puts")

    def _emit_progress(component: str, step: int, label: str, day_iso: str, expiry_iso: str) -> None:
        if on_event is None:
            return
        on_event(
            {
                "type": "bundle_progress",
                "component": component,
                "step": step,
                "label": label,
                "day": day_iso,
                "expiry": expiry_iso,
            }
        )

    def _process_side(
        side: str,  # "call" | "put"
        contracts_for_day: list[dict[str, Any]],
        prior_close: float,
        target_expiry: date,
        day: date,
        day_iso: str,
        expiry_iso: str,
    ) -> int:
        nonlocal calls_done, puts_done
        slotted = _select_strikes_with_slots(contracts_for_day, prior_close, config.strikes_each_side)
        component = "calls" if side == "call" else "puts"
        for offset, c in slotted:
            _check_cancel()
            contract = _SelectedContract(
                ticker=c["ticker"],
                strike=float(c["strike_price"]),
                expiration=target_expiry,
                contract_type=side,
                slot_offset=offset,
            )
            if side == "call":
                calls_done += 1
                _emit_progress(component, calls_done, contract.ticker, day_iso, expiry_iso)
            else:
                puts_done += 1
                _emit_progress(component, puts_done, contract.ticker, day_iso, expiry_iso)
            rows = _process_contract(contract, day, to_date, underlying_by_ts, config, timespan, multiplier, polygon)
            if rows:
                slot_rows.setdefault((side, offset), []).extend(rows)
        return len(slotted)

    for i, day in enumerate(trading_days):
        _check_cancel()
        prior_close = first_day_close if i == 0 else prior_close_by_day[trading_days[i - 1]]

        target_expiry = _resolve_target_expiry(polygon, ticker, day, config)
        if target_expiry is None:
            skipped_days.append(
                {
                    "date": day.isoformat(),
                    "reason": f"no listed expiry at trading_day + {config.dte_distance} days",
                }
            )
            continue

        day_iso = day.isoformat()
        expiry_iso = target_expiry.isoformat()
        day_report: dict[str, Any] = {
            "date": day_iso,
            "expiry": expiry_iso,
            "prior_close": prior_close,
            "calls_selected": 0,
            "puts_selected": 0,
        }

        if config.include_calls:
            calls = _fetch_contracts_for_expiry(polygon, ticker, day, target_expiry, "call")
            day_report["calls_selected"] = _process_side(
                "call", calls, prior_close, target_expiry, day, day_iso, expiry_iso
            )

        if config.include_puts:
            puts = _fetch_contracts_for_expiry(polygon, ticker, day, target_expiry, "put")
            day_report["puts_selected"] = _process_side(
                "put", puts, prior_close, target_expiry, day, day_iso, expiry_iso
            )

        per_day_report.append(day_report)

    # Sort, tag discontinuity, and serialize each non-empty slot file.
    columns = _columns_for(config)
    slot_files: dict[str, bytes] = {}
    for (side, offset), rows in slot_rows.items():
        rows.sort(key=lambda r: (r["unix_ts"], r["contract_ticker"]))
        if config.include_discontinuity:
            _mark_discontinuity(rows)
        folder = "calls" if side == "call" else "puts"
        path = f"{folder}/{_slot_label(offset)}.csv"
        slot_files[path] = _write_rows_to_csv(rows, columns)

    total_call_rows = sum(len(v) for k, v in slot_rows.items() if k[0] == "call")
    total_put_rows = sum(len(v) for k, v in slot_rows.items() if k[0] == "put")

    report = {
        "enabled": True,
        "ticker": ticker,
        "from_date": from_date,
        "to_date": to_date,
        "timespan": timespan,
        "multiplier": multiplier,
        "dte_distance": config.dte_distance,
        "strikes_each_side": config.strikes_each_side,
        "calls_rows": total_call_rows,
        "puts_rows": total_put_rows,
        "calls_files": sorted(p for p in slot_files if p.startswith("calls/")),
        "puts_files": sorted(p for p in slot_files if p.startswith("puts/")),
        "days_processed": len(per_day_report),
        "days_skipped": skipped_days,
        "per_day": per_day_report,
    }
    logger.info(
        f"[OC] Built companion for {ticker}: "
        f"{total_call_rows} call rows across {len(report['calls_files'])} files, "
        f"{total_put_rows} put rows across {len(report['puts_files'])} files, "
        f"{len(per_day_report)} days, {len(skipped_days)} skipped"
    )
    return slot_files, report
