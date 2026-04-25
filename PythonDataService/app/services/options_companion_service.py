"""Options companion CSV builder.

Given already-fetched underlying minute bars plus an ``OptionsCompanionConfig``,
this module discovers option contracts around the ATM strike on each target
expiry date, fetches 1-minute aggregates for each contract, and computes
implied volatility and selected Greeks *per bar* via QuantLib. The result is
one CSV per option type (calls, puts) in long format.

IV source is deterministically solved per bar via
``app.volatility.solver.implied_volatility`` (QuantLib primary, Brent
fallback) — the volatility surface is intentionally **not** used as an input
in v1 because surface-based IV requires pre-building a surface per minute
timestamp per day, which is impractical. Surface-based IV is the future
cross-check, not the input (see docs/references for the deferred validation
task).

Per-bar Greek values are pending a formal validation pass against LEAN /
QuantLib's analytic engine (see the project plan) — today we rely on
QuantLib's ``AnalyticEuropeanEngine`` for delta/gamma/theta/vega/rho.
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


def _select_strikes(
    contracts: list[dict[str, Any]],
    prior_close: float,
    strikes_each_side: int,
) -> list[dict[str, Any]]:
    """Pick ATM + N strikes above + N below sorted by strike."""
    if not contracts:
        return []

    sorted_c = sorted(contracts, key=lambda c: c["strike_price"])
    strikes = [c["strike_price"] for c in sorted_c]

    # Find ATM index via closest strike
    atm_idx = int(np.argmin(np.abs(np.asarray(strikes) - prior_close)))

    lo = max(0, atm_idx - strikes_each_side)
    hi = min(len(sorted_c), atm_idx + strikes_each_side + 1)
    return sorted_c[lo:hi]


def _resolve_target_expiry(
    polygon: PolygonClientService,
    ticker: str,
    trading_day: date,
    config: OptionsCompanionConfig,
) -> date | None:
    """Return the expiry date to use for a given `trading_day`, or None if none available."""
    if config.expiry_mode == "same_day":
        return trading_day

    # nearest_within_days: pull expirations for [day, day + max_dte]
    end = trading_day + timedelta(days=config.max_dte)
    try:
        expirations = polygon.list_options_expirations(
            underlying_ticker=ticker,
            expiration_date_gte=trading_day.isoformat(),
            expiration_date_lte=end.isoformat(),
        )
    except Exception as exc:
        logger.warning(f"[OC] Expiration lookup failed for {ticker} on {trading_day}: {exc}")
        return None

    if not expirations:
        return None

    # list_options_expirations returns strings; sort and take earliest ≥ trading_day
    candidates = sorted({e for e in expirations if e >= trading_day.isoformat()})
    if not candidates:
        return None
    return date.fromisoformat(candidates[0])


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

    # Compute Greeks using the solved IV. We skip this path if QuantLib is not installed
    # — IV still populates (it has its own fallback), but Greeks require QL for now.
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
    """Return the ordered list of columns for a companion CSV."""
    cols = ["unix_ts", "iso_time", "contract_ticker", "expiration", "strike", "type"]
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


def _sort_companion_rows(rows: list[dict[str, Any]]) -> None:
    """Sort companion rows in place by ``(unix_ts, contract_ticker)``.

    Bars accumulate in append order (day → strike → bar), producing
    contract-grouped, time-resetting blocks. This sort flattens them into a
    single chronological stream and breaks ties on contract ticker for
    determinism (multiple contracts always share each bar timestamp).
    """
    rows.sort(key=lambda r: (r["unix_ts"], r["contract_ticker"]))


def _write_rows_to_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    """Serialize rows to CSV bytes.

    Caller contract: ``rows`` must already be sorted by their canonical time
    key (``unix_ts`` for option bars). Use :func:`_sort_companion_rows`
    immediately before calling this writer.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_fmt(row.get(c)) for c in columns])
    return buf.getvalue().encode("utf-8")


def _process_contract(
    contract: _SelectedContract,
    trading_day: date,
    from_date: str,
    to_date: str,
    underlying_by_ts: dict[int, float],
    config: OptionsCompanionConfig,
    timespan: str,
    multiplier: int,
    polygon: PolygonClientService,
) -> list[dict[str, Any]]:
    """Fetch the option's aggregate bars at the ticker's resolution and build one row per bar.

    Fetch window is clamped to ``[trading_day, min(trading_day + expiry_span, to_date)]``
    — the option only produces bars while it's live, and for ``same_day`` mode
    that's a single trading date.
    """
    # Clamp fetch window to the contract's lifetime within [from_date, to_date]
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
            "type": contract.contract_type,
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
) -> tuple[bytes | None, bytes | None, dict[str, Any]]:
    """Build options_calls.csv and/or options_puts.csv byte payloads plus a summary report.

    Options bars are fetched at the same ``(timespan, multiplier)`` as the
    underlying — Polygon serves both through the same aggregate endpoint on
    the same UTC-anchored bar grid, so timestamps align exactly. The IV-solve
    keys the underlying lookup map by a floored bar-start so any future grid
    drift still produces correct rows (the row simply loses IV/Greeks rather
    than pairing with a wrong spot).

    Returns
    -------
    (calls_bytes, puts_bytes, report)
        Either byte tuple entry is ``None`` when the corresponding type is
        disabled or no rows were produced. ``report`` captures skipped days,
        chosen expiries, and contract counts for the metadata bundle.
    """
    if not config.enabled or underlying_bars_df.empty:
        return None, None, {"enabled": False, "reason": "disabled or empty bars"}

    # Imported lazily to avoid a circular import: options_companion_service is
    # imported from app.routers.dataset, which itself imports from
    # dataset_service. Pulling RunCancelledError at function-call time breaks
    # the cycle without changing module graph layout.
    from app.services.dataset_service import RunCancelledError

    prior_close_by_day = _prior_day_close_map(underlying_bars_df)
    underlying_by_ts = _underlying_close_map(underlying_bars_df, timespan, multiplier)

    # Unique trading days present in the dataset (ET date).
    trading_days = sorted(prior_close_by_day.keys())
    if not trading_days:
        return None, None, {"enabled": True, "reason": "no trading days derived from bars"}

    # Seed prior-close for the first day: fall back to the first bar's close on that day
    # if we don't have a real prior day.
    first_day_close = None
    if len(trading_days) >= 2:
        first_day_close = prior_close_by_day[trading_days[0]]
    else:
        first_day_close = float(underlying_bars_df["close"].iloc[0])

    calls_rows: list[dict[str, Any]] = []
    puts_rows: list[dict[str, Any]] = []
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

    for i, day in enumerate(trading_days):
        _check_cancel()
        if i == 0:
            prior_close = first_day_close
        else:
            prior_close = prior_close_by_day[trading_days[i - 1]]

        target_expiry = _resolve_target_expiry(polygon, ticker, day, config)
        if target_expiry is None:
            skipped_days.append({"date": day.isoformat(), "reason": "no expiry found"})
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
            selected_calls = _select_strikes(calls, prior_close, config.strikes_each_side)
            day_report["calls_selected"] = len(selected_calls)
            for c in selected_calls:
                _check_cancel()
                contract = _SelectedContract(
                    ticker=c["ticker"],
                    strike=float(c["strike_price"]),
                    expiration=target_expiry,
                    contract_type="call",
                )
                calls_done += 1
                _emit_progress("options_calls.csv", calls_done, contract.ticker, day_iso, expiry_iso)
                calls_rows.extend(
                    _process_contract(
                        contract, day, from_date, to_date, underlying_by_ts, config, timespan, multiplier, polygon
                    )
                )

        if config.include_puts:
            puts = _fetch_contracts_for_expiry(polygon, ticker, day, target_expiry, "put")
            selected_puts = _select_strikes(puts, prior_close, config.strikes_each_side)
            day_report["puts_selected"] = len(selected_puts)
            for c in selected_puts:
                _check_cancel()
                contract = _SelectedContract(
                    ticker=c["ticker"],
                    strike=float(c["strike_price"]),
                    expiration=target_expiry,
                    contract_type="put",
                )
                puts_done += 1
                _emit_progress("options_puts.csv", puts_done, contract.ticker, day_iso, expiry_iso)
                puts_rows.extend(
                    _process_contract(
                        contract, day, from_date, to_date, underlying_by_ts, config, timespan, multiplier, polygon
                    )
                )

        per_day_report.append(day_report)

    # Globally sort by bar timestamp so the CSV is monotonic in time.
    _sort_companion_rows(calls_rows)
    _sort_companion_rows(puts_rows)

    columns = _columns_for(config)
    calls_bytes = _write_rows_to_csv(calls_rows, columns) if (config.include_calls and calls_rows) else None
    puts_bytes = _write_rows_to_csv(puts_rows, columns) if (config.include_puts and puts_rows) else None

    report = {
        "enabled": True,
        "ticker": ticker,
        "from_date": from_date,
        "to_date": to_date,
        "timespan": timespan,
        "multiplier": multiplier,
        "expiry_mode": config.expiry_mode,
        "strikes_each_side": config.strikes_each_side,
        "calls_rows": len(calls_rows),
        "puts_rows": len(puts_rows),
        "days_processed": len(per_day_report),
        "days_skipped": skipped_days,
        "per_day": per_day_report,
    }
    logger.info(
        f"[OC] Built companion for {ticker}: "
        f"{len(calls_rows)} call rows, {len(puts_rows)} put rows, "
        f"{len(per_day_report)} days, {len(skipped_days)} skipped"
    )
    return calls_bytes, puts_bytes, report
