"""A Black-Scholes option chain in the shape the recorder reads from the vendor's snapshot (#3525).

Every contract's mid is the model price at ``sigma`` (so the recorder's IV30 should come back near it), with a fixed
half-spread. ``quote_ns`` stamps each contract's ``last_quote.last_updated`` the way the vendor does, in nanoseconds
since the epoch; omitted, the contracts carry none.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.options.expiry import option_expiry_anchor_ms
from app.services.bs_greeks import bs_european_price
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import to_ms_utc


def bs_chain_payload(
    *, spot: float, sigma: float, rate: float, asof: datetime,
    expiry_days: list[int], strikes: list[float], half_spread: float = 0.05,
    quote_ns: int | None = None,
) -> dict:
    contracts = []
    for d in expiry_days:
        T = d / 365.0
        exp_iso = (et_date_at_ms(to_ms_utc(asof)) + timedelta(days=d)).isoformat()
        for k in strikes:
            for is_call in (True, False):
                price = bs_european_price(
                    spot=spot, strike=k, ttm_years=T, rate=rate,
                    volatility=sigma, is_call=is_call,
                )
                quote = {
                    "bid": float(max(0.0, price - half_spread)),
                    "ask": float(price + half_spread),
                }
                if quote_ns is not None:
                    quote["last_updated"] = quote_ns
                contracts.append(
                    {
                        "ticker": f"O:SPY{d}{'C' if is_call else 'P'}{int(k*100):08d}",
                        "contract_type": "call" if is_call else "put",
                        "strike_price": float(k),
                        "expiration_ms": option_expiry_anchor_ms(date.fromisoformat(exp_iso)),
                        "last_quote": quote,
                        "implied_volatility": float(sigma),
                    }
                )
    return {
        "underlying": {"ticker": "SPY", "price": spot},
        "contracts": contracts,
    }
