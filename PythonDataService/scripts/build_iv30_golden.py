"""Populate the SPY IV30 golden fixture for VIX-replication parity test.

Output:
    PythonDataService/tests/fixtures/golden/iv30/spy-2024-12-20-chain.parquet
    PythonDataService/tests/fixtures/golden/iv30/spy-2024-12-20-chain.meta.json

Method:
    1. Fetch SPY underlying close on 2024-12-20 (spot proxy).
    2. Enumerate SPY option contracts as-of 2024-12-20 with expiry within
       21–60 calendar days (covers the two expiries straddling 30 days).
    3. For each contract, fetch the daily aggregate on 2024-12-20.
    4. Group by expiry, pivot calls vs puts at each strike, build OptionQuote
       rows. Polygon Starter daily bars don't carry bid/ask, so we synthesize
       ±half_spread around close (max($0.05, 0.5% of close)) and treat any
       contract with close < $0.05 as zero-bid (matches CBOE truncation rule).
    5. Solve our parametric IV30 from the same chain and stash both numbers
       in the meta sidecar. The golden test asserts our parametric agrees
       with the VIX replication within 50 bps.

Run inside the python container:
    podman exec polygon-data-service python /app/scripts/build_iv30_golden.py
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import pandas as pd

from app.services.polygon_client import PolygonClientService
from app.services.rate_dividend_service import get_rate_and_dividend
from app.volatility.solver import implied_volatility
from app.volatility.vix_replication import OptionQuote, vix_style_iv30
from app.engine.edge.features_realtime.iv30_constructor import iv30_atm_50d

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GOLDEN_DATE = "2024-12-20"
SYMBOL = "SPY"
OUT_DIR = Path("/app/tests/fixtures/golden/iv30")
OUT_PARQUET = OUT_DIR / "spy-2024-12-20-chain.parquet"
OUT_META = OUT_DIR / "spy-2024-12-20-chain.meta.json"


def _half_spread(close: float) -> float:
    """Synthetic half-spread when bid/ask not in daily aggregates.
    Floor $0.05, scale 0.5% of close above that — matches OPRA-typical SPY width.
    """
    return max(0.05, 0.005 * close)


def fetch_chain(polygon: PolygonClientService) -> tuple[float, list[dict]]:
    """Return (spot_close, list of {strike, expiry_days, contract_type, close}).
    Filters to expiries 21–60 calendar days out (straddles 30)."""
    aggs = polygon.fetch_aggregates(
        ticker=SYMBOL, multiplier=1, timespan="day",
        from_date=GOLDEN_DATE, to_date=GOLDEN_DATE,
    )
    if not aggs:
        raise RuntimeError(f"no SPY aggregate for {GOLDEN_DATE}")
    spot = float(aggs[0]["close"])
    logger.info("SPY close on %s: %.2f", GOLDEN_DATE, spot)

    target_dt = pd.Timestamp(GOLDEN_DATE)
    exp_lo = (target_dt + pd.Timedelta(days=21)).strftime("%Y-%m-%d")
    exp_hi = (target_dt + pd.Timedelta(days=60)).strftime("%Y-%m-%d")

    contracts = polygon.list_options_contracts(
        underlying_ticker=SYMBOL,
        as_of_date=GOLDEN_DATE,
        expiration_date_gte=exp_lo,
        expiration_date_lte=exp_hi,
        limit=2000,
    )
    logger.info("contracts to query: %d", len(contracts))

    rows: list[dict] = []
    for i, c in enumerate(contracts):
        if i % 50 == 0:
            logger.info("  fetched %d/%d", i, len(contracts))
        try:
            day = polygon.fetch_aggregates(
                ticker=c["ticker"], multiplier=1, timespan="day",
                from_date=GOLDEN_DATE, to_date=GOLDEN_DATE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("  skip %s: %s", c["ticker"], exc)
            continue
        if not day:
            continue
        close = float(day[0]["close"])
        rows.append({
            "ticker": c["ticker"],
            "strike": float(c["strike_price"]),
            "expiration_date": c["expiration_date"],
            "expiry_days": (pd.Timestamp(c["expiration_date"]) - target_dt).days,
            "contract_type": c["contract_type"],
            "close": close,
        })
    return spot, rows


def build_quotes_per_expiry(rows: list[dict]) -> dict[int, list[OptionQuote]]:
    """Pivot rows into {expiry_days: [OptionQuote]} keyed by strike."""
    df = pd.DataFrame(rows)
    out: dict[int, list[OptionQuote]] = {}
    for expiry_days, grp in df.groupby("expiry_days"):
        wide = grp.pivot_table(
            index="strike", columns="contract_type", values="close", aggfunc="first"
        )
        wide = wide.dropna(subset=["call", "put"], how="all").sort_index()
        quotes: list[OptionQuote] = []
        for strike, row in wide.iterrows():
            call = float(row.get("call", 0.0)) if not pd.isna(row.get("call")) else 0.0
            put = float(row.get("put", 0.0)) if not pd.isna(row.get("put")) else 0.0
            half = _half_spread(max(call, put, 1.0))
            call_bid = max(0.0, call - half) if call >= 0.05 else 0.0
            call_ask = call + half if call > 0 else 0.0
            put_bid = max(0.0, put - half) if put >= 0.05 else 0.0
            put_ask = put + half if put > 0 else 0.0
            quotes.append(
                OptionQuote(
                    strike=float(strike),
                    call_bid=call_bid, call_ask=call_ask,
                    put_bid=put_bid, put_ask=put_ask,
                )
            )
        out[int(expiry_days)] = quotes
    return out


def parametric_iv30(rows: list[dict], spot: float, rate: float, dividend: float) -> float:
    """Solve per-contract IV, take ATM (closest-to-spot) per expiry, variance-interpolate to 30d."""
    df = pd.DataFrame(rows)
    target_dt = pd.Timestamp(GOLDEN_DATE)
    iv_by_expiry: dict[int, float] = {}
    for expiry_days, grp in df.groupby("expiry_days"):
        wide = grp.pivot_table(
            index="strike", columns="contract_type", values="close", aggfunc="first"
        ).dropna(subset=["call"]).sort_index()
        if wide.empty:
            continue
        # ATM strike = closest to spot.
        strikes = wide.index.to_numpy()
        atm_idx = (abs(strikes - spot)).argmin()
        atm_strike = float(strikes[atm_idx])
        atm_call = float(wide.loc[atm_strike, "call"])
        ttm = expiry_days / 365.0
        if ttm <= 0:
            continue
        res = implied_volatility(
            option_price=atm_call,
            spot=spot, strike=atm_strike, ttm=ttm,
            rate=rate, dividend=dividend, is_call=True,
        )
        if res.iv is not None and res.iv > 0:
            iv_by_expiry[int(expiry_days)] = float(res.iv)
    if not iv_by_expiry:
        raise RuntimeError("no per-expiry IVs solved")
    series = pd.Series(iv_by_expiry).sort_index()
    val = iv30_atm_50d(series, target_days=30)
    if val is None:
        raise RuntimeError(f"iv30_atm_50d failed on series: {series}")
    return float(val)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    polygon = PolygonClientService()

    spot, rows = fetch_chain(polygon)
    logger.info("rows with daily aggregates: %d", len(rows))

    rd = get_rate_and_dividend(
        ticker=SYMBOL, spot_price=spot, polygon=polygon,
        dte_days=30, observation_date=GOLDEN_DATE,
    )
    logger.info("rate=%.4f dividend=%.4f (%s / %s)", rd.rate, rd.dividend_yield, rd.source_rate, rd.source_dividend)

    quotes_by_expiry = build_quotes_per_expiry(rows)
    expiries = sorted(quotes_by_expiry.keys())
    below = max([e for e in expiries if e <= 30], default=None)
    above = min([e for e in expiries if e >= 30], default=None)
    if below is None or above is None:
        raise RuntimeError(f"no straddling expiries in {expiries}")
    logger.info("straddling expiries: %dd / %dd", below, above)

    sigma_vix = vix_style_iv30(
        quotes_by_expiry[below], quotes_by_expiry[above],
        rate1=rd.rate, T1_calendar_days=below,
        rate2=rd.rate, T2_calendar_days=above,
        target_calendar_days=30,
    )
    sigma_param = parametric_iv30(rows, spot, rd.rate, rd.dividend_yield)
    logger.info("σ_VIX=%.4f σ_param=%.4f Δ=%.0f bps", sigma_vix, sigma_param, abs(sigma_vix - sigma_param) * 10000)

    df = pd.DataFrame(rows)
    df.to_parquet(OUT_PARQUET, engine="pyarrow", compression="snappy")
    logger.info("wrote %s (%d rows)", OUT_PARQUET, len(df))

    meta = {
        "source": "Polygon /v3/reference/options/contracts + /v2/aggs",
        "as_of_date": GOLDEN_DATE,
        "symbol": SYMBOL,
        "spot": spot,
        "rate": rd.rate,
        "dividend": rd.dividend_yield,
        "rate_source": rd.source_rate,
        "dividend_source": rd.source_dividend,
        "expiries_in_window": expiries,
        "straddle": {"below_30d": below, "above_30d": above},
        "vix_style_iv30_act365": sigma_vix,
        "parametric_iv30": sigma_param,
        "iv30_diff_bps": abs(sigma_vix - sigma_param) * 10000,
        "half_spread_policy": "max($0.05, 0.5% of close); zero-bid below $0.05",
        "n_contracts": len(rows),
        "schema": {
            "expiry_days": "int — calendar days to expiry from as_of_date",
            "strike": "float — strike price",
            "expiration_date": "ISO date string",
            "contract_type": "'call' | 'put'",
            "close": "float — Polygon daily close as of as_of_date",
            "ticker": "Polygon contract ticker (O:SPY...)",
        },
    }
    OUT_META.write_text(json.dumps(meta, indent=2))
    logger.info("wrote %s", OUT_META)


if __name__ == "__main__":
    main()
