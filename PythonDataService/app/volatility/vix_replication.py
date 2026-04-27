"""VIX-style variance replication of constant-maturity IV30.

Implements the CBOE VIX 2019 whitepaper formula on a chain of listed options.
Used as **ground truth** for our parametric IV30 (which interpolates fitted
ATM σ in variance-space) — Step 4 of the IV-RV alignment plan.

Formula per expiry T:

    σ²_T = (2/T) · Σ_i (ΔK_i / K_i²) · e^(rT) · Q(K_i)
         - (1/T) · (F/K_0 - 1)²

where:
    F     = K* + e^(rT) · (C(K*) - P(K*)), K* = arg min_K |C-P|
    K_0   = max{K : K ≤ F}
    Q(K)  = put mid for K < K_0; call mid for K > K_0; average at K = K_0
    ΔK_i  = (K_{i+1} - K_{i-1}) / 2 (interior); single forward/back diff at edges

Strike walk: outward from K_0; stop after **two consecutive zero-bid strikes**
in each direction. Constant-maturity 30-day vol is a variance-time
interpolation between the two expiries straddling 30 calendar days.

Output is annualized vol on ACT/365 basis (matches the solver). Use
:func:`app.volatility.basis.convert_iv_act365_to_trading252` for VRP.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OptionQuote:
    """Single-strike quote — both call and put bid/ask on the same expiry."""

    strike: float
    call_bid: float
    call_ask: float
    put_bid: float
    put_ask: float

    @property
    def call_mid(self) -> float:
        return (self.call_bid + self.call_ask) / 2.0 if self.call_ask > 0 else 0.0

    @property
    def put_mid(self) -> float:
        return (self.put_bid + self.put_ask) / 2.0 if self.put_ask > 0 else 0.0


@dataclass(frozen=True)
class ExpiryReplication:
    T_years: float
    forward: float
    K0: float
    sigma_squared_T: float  # annualized variance σ²(T) — needed for time interpolation
    n_strikes_used: int


def _select_atm_strike(quotes: list[OptionQuote]) -> OptionQuote:
    """Smallest |call_mid - put_mid| among quotes with both sides quoted."""
    best, best_diff = None, float("inf")
    for q in quotes:
        if q.call_mid <= 0 or q.put_mid <= 0:
            continue
        diff = abs(q.call_mid - q.put_mid)
        if diff < best_diff:
            best_diff, best = diff, q
    if best is None:
        raise ValueError("no strike with both call and put mids > 0")
    return best


def replicate_expiry_variance(
    quotes: list[OptionQuote],
    *,
    rate: float,
    T_years: float,
) -> ExpiryReplication:
    """Compute σ²(T) for one expiry from CBOE-VIX whitepaper formula."""
    if T_years <= 0:
        raise ValueError(f"T_years must be > 0: {T_years}")
    if not quotes:
        raise ValueError("quotes must not be empty")

    sorted_quotes = sorted(quotes, key=lambda q: q.strike)
    strikes = [q.strike for q in sorted_quotes]

    # Forward via put-call parity at the ATM strike.
    atm = _select_atm_strike(sorted_quotes)
    forward = atm.strike + math.exp(rate * T_years) * (atm.call_mid - atm.put_mid)

    # K_0 = highest strike at or below the forward.
    below_or_eq = [k for k in strikes if k <= forward]
    K0 = max(below_or_eq) if below_or_eq else strikes[0]
    K0_idx = strikes.index(K0)

    # Walk outward; stop after two consecutive zero-bid strikes per direction.
    selected: list[tuple[OptionQuote, str]] = []  # (quote, "put"|"call"|"both")

    consec_zero = 0
    for i in range(K0_idx - 1, -1, -1):
        q = sorted_quotes[i]
        if q.put_bid <= 0:
            consec_zero += 1
            if consec_zero >= 2:
                break
            continue
        consec_zero = 0
        selected.append((q, "put"))

    selected.append((sorted_quotes[K0_idx], "both"))

    consec_zero = 0
    for i in range(K0_idx + 1, len(sorted_quotes)):
        q = sorted_quotes[i]
        if q.call_bid <= 0:
            consec_zero += 1
            if consec_zero >= 2:
                break
            continue
        consec_zero = 0
        selected.append((q, "call"))

    selected.sort(key=lambda pair: pair[0].strike)
    sel_strikes = [q.strike for q, _ in selected]
    n = len(sel_strikes)
    if n < 3:
        raise ValueError(f"need at least 3 selected strikes, got {n}")

    e_rT = math.exp(rate * T_years)
    var_sum = 0.0
    for i, (q, kind) in enumerate(selected):
        if i == 0:
            dK = sel_strikes[1] - sel_strikes[0]
        elif i == n - 1:
            dK = sel_strikes[-1] - sel_strikes[-2]
        else:
            dK = (sel_strikes[i + 1] - sel_strikes[i - 1]) / 2.0
        if kind == "put":
            Q = q.put_mid
        elif kind == "call":
            Q = q.call_mid
        else:  # both
            Q = (q.call_mid + q.put_mid) / 2.0
        var_sum += (dK / (q.strike**2)) * e_rT * Q

    sigma_sq_T = (2.0 / T_years) * var_sum - (1.0 / T_years) * (forward / K0 - 1.0) ** 2
    return ExpiryReplication(
        T_years=T_years,
        forward=forward,
        K0=K0,
        sigma_squared_T=sigma_sq_T,
        n_strikes_used=n,
    )


def vix_style_iv30(
    expiry1_quotes: list[OptionQuote],
    expiry2_quotes: list[OptionQuote],
    *,
    rate1: float,
    T1_calendar_days: int,
    rate2: float,
    T2_calendar_days: int,
    target_calendar_days: int = 30,
) -> float:
    """Constant-maturity VIX-style σ at ``target_calendar_days``.

    Two expiries straddle the target (T1 ≤ target ≤ T2). Returns annualized
    σ on ACT/365 basis. Variance-time interpolation:

        σ²_target · T_target = w · σ²_T1 · T1 + (1-w) · σ²_T2 · T2
        w = (T2 - T_target) / (T2 - T1)
    """
    if not (T1_calendar_days <= target_calendar_days <= T2_calendar_days):
        raise ValueError(
            f"target {target_calendar_days} not bracketed by [{T1_calendar_days}, {T2_calendar_days}]"
        )
    if T1_calendar_days == T2_calendar_days:
        raise ValueError("expiries must differ — got equal T1 and T2")

    T1 = T1_calendar_days / 365.0
    T2 = T2_calendar_days / 365.0
    T_target = target_calendar_days / 365.0

    rep1 = replicate_expiry_variance(expiry1_quotes, rate=rate1, T_years=T1)
    rep2 = replicate_expiry_variance(expiry2_quotes, rate=rate2, T_years=T2)

    w = (T2 - T_target) / (T2 - T1)
    var_target_times_T = w * rep1.sigma_squared_T * T1 + (1.0 - w) * rep2.sigma_squared_T * T2
    sigma_squared = var_target_times_T / T_target
    if sigma_squared <= 0:
        raise ValueError(f"replicated variance non-positive: {sigma_squared}")
    return math.sqrt(sigma_squared)
