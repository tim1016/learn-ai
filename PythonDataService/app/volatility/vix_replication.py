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

from app.volatility.iv_provenance import IvProvenance
from app.volatility.price_normalization import (
    NormalizedOptionPrice,
    NormalizedOptionQuote,
    PriceSource,
    from_snapshot_quote,
)


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


def wrap_legacy_as_opra_mid(quote: OptionQuote) -> NormalizedOptionQuote:
    """Transitional helper: wrap a legacy ``OptionQuote`` as a
    ``NormalizedOptionQuote`` whose legs are tagged ``opra_mid``.

    Use only when the caller knows the legacy ``OptionQuote`` represents real
    OPRA bid/ask. For EOD-close-derived data, the call site should construct
    ``NormalizedOptionPrice`` via ``from_eod_close`` directly so the synthetic
    spread provenance is preserved.
    """
    return NormalizedOptionQuote(
        strike=quote.strike,
        call=from_snapshot_quote(quote.call_bid, quote.call_ask),
        put=from_snapshot_quote(quote.put_bid, quote.put_ask),
    )


def _select_atm_strike_normalized(quotes: list[NormalizedOptionQuote]) -> NormalizedOptionQuote:
    """Smallest |call_mid - put_mid| among quotes with both legs non-zero-bid."""
    best, best_diff = None, float("inf")
    for nq in quotes:
        if nq.call.is_zero_bid or nq.put.is_zero_bid:
            continue
        diff = abs(nq.call.mid - nq.put.mid)
        if diff < best_diff:
            best_diff, best = diff, nq
    if best is None:
        raise ValueError("no strike with both call and put mids > 0")
    return best


def replicate_expiry_variance_with_provenance(
    quotes: list[NormalizedOptionQuote],
    *,
    rate: float,
    T_years: float,
    debug: bool = False,
) -> tuple[ExpiryReplication, IvProvenance]:
    """Same math as ``replicate_expiry_variance`` plus ``IvProvenance``.

    The math is duplicated rather than refactored to keep both paths
    independently auditable: the legacy bare-float entry point stays in
    place, and this provenance-aware path is opt-in. Both follow the CBOE
    VIX 2019 whitepaper algorithm to the letter.

    Provenance includes:

    - ``price_source_mix``: count-based share of each ``PriceSource`` across
      the legs actually used in the integration.
    - ``variance_contribution_synthetic``: variance-weighted share of
      synthetic-spread contribution. This is the operational metric for
      gating downstream signals — the count-based share would understate
      synthesis when the wings (high `c_i`) are synthetic and the ATM is
      real.
    - ``strike_coverage_score``: ``min(1, avg_wings_in_sigma / 5)``, where
      σ is the replicated σ_T expressed in dollar units at expiration.
    - ``per_strike_contributions``: opt-in via ``debug=True``.
    """
    if T_years <= 0:
        raise ValueError(f"T_years must be > 0: {T_years}")
    if not quotes:
        raise ValueError("quotes must not be empty")

    sorted_q = sorted(quotes, key=lambda nq: nq.strike)
    strikes = [nq.strike for nq in sorted_q]

    atm = _select_atm_strike_normalized(sorted_q)
    forward = atm.strike + math.exp(rate * T_years) * (atm.call.mid - atm.put.mid)

    below_or_eq = [k for k in strikes if k <= forward]
    K0 = max(below_or_eq) if below_or_eq else strikes[0]
    K0_idx = strikes.index(K0)

    # Walk outward; stop after two consecutive zero-bid strikes per direction.
    selected: list[tuple[NormalizedOptionQuote, str]] = []

    consec_zero = 0
    for i in range(K0_idx - 1, -1, -1):
        nq = sorted_q[i]
        if nq.put.is_zero_bid:
            consec_zero += 1
            if consec_zero >= 2:
                break
            continue
        consec_zero = 0
        selected.append((nq, "put"))

    selected.append((sorted_q[K0_idx], "both"))

    consec_zero = 0
    for i in range(K0_idx + 1, len(sorted_q)):
        nq = sorted_q[i]
        if nq.call.is_zero_bid:
            consec_zero += 1
            if consec_zero >= 2:
                break
            continue
        consec_zero = 0
        selected.append((nq, "call"))

    selected.sort(key=lambda pair: pair[0].strike)
    sel_strikes = [nq.strike for nq, _ in selected]
    n = len(sel_strikes)
    if n < 3:
        raise ValueError(f"need at least 3 selected strikes, got {n}")

    e_rT = math.exp(rate * T_years)
    var_sum = 0.0
    contrib_total = 0.0
    contrib_synthetic = 0.0
    count_per_source: dict[PriceSource, float] = {}
    contributions: list[dict] = []

    for i, (nq, kind) in enumerate(selected):
        if i == 0:
            dK = sel_strikes[1] - sel_strikes[0]
        elif i == n - 1:
            dK = sel_strikes[-1] - sel_strikes[-2]
        else:
            dK = (sel_strikes[i + 1] - sel_strikes[i - 1]) / 2.0

        if kind == "put":
            Q = nq.put.mid
            active_legs: list[tuple[NormalizedOptionPrice, float]] = [(nq.put, 1.0)]
        elif kind == "call":
            Q = nq.call.mid
            active_legs = [(nq.call, 1.0)]
        else:  # "both" — at K0, average ATM call and put
            Q = (nq.call.mid + nq.put.mid) / 2.0
            active_legs = [(nq.call, 0.5), (nq.put, 0.5)]

        # The integral term per strike, before the (forward/K0 - 1)² adjustment.
        c_i = (2.0 / T_years) * (dK / (nq.strike**2)) * e_rT * Q
        var_sum += (dK / (nq.strike**2)) * e_rT * Q
        contrib_total += c_i

        any_synthetic = False
        for leg, frac in active_legs:
            if leg.spread_synthetic:
                contrib_synthetic += c_i * frac
                any_synthetic = True
            count_per_source[leg.source] = count_per_source.get(leg.source, 0.0) + frac

        if debug:
            contributions.append(
                {
                    "strike": nq.strike,
                    "kind": kind,
                    "dK": dK,
                    "Q": Q,
                    "c_i": c_i,
                    "active_leg_sources": [leg.source for leg, _ in active_legs],
                    "active_leg_synthetic": any_synthetic,
                }
            )

    sigma_sq_T = (2.0 / T_years) * var_sum - (1.0 / T_years) * (forward / K0 - 1.0) ** 2
    rep = ExpiryReplication(
        T_years=T_years,
        forward=forward,
        K0=K0,
        sigma_squared_T=sigma_sq_T,
        n_strikes_used=n,
    )

    vcs = contrib_synthetic / contrib_total if contrib_total > 0 else 0.0
    vcs = max(0.0, min(1.0, vcs))

    total_count = sum(count_per_source.values())
    price_source_mix: dict[PriceSource, float] = (
        {src: cnt / total_count for src, cnt in count_per_source.items()}
        if total_count > 0
        else {}
    )

    sigma_T = math.sqrt(max(sigma_sq_T, 0.0))
    sigma_dollar = forward * sigma_T * math.sqrt(T_years)
    if sigma_dollar > 0:
        upper_sigmas = max(0.0, (max(sel_strikes) - forward) / sigma_dollar)
        lower_sigmas = max(0.0, (forward - min(sel_strikes)) / sigma_dollar)
        avg_sigmas = (upper_sigmas + lower_sigmas) / 2.0
        strike_coverage = min(1.0, avg_sigmas / 5.0)
    else:
        strike_coverage = 0.0

    prov = IvProvenance(
        iv_source="internal_solver",
        price_source_mix=price_source_mix,
        variance_contribution_synthetic=vcs,
        strike_coverage_score=strike_coverage,
        per_strike_contributions=contributions if debug else None,
    )

    return rep, prov


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


def vix_style_iv30_with_provenance(
    expiry1_quotes: list[NormalizedOptionQuote],
    expiry2_quotes: list[NormalizedOptionQuote],
    *,
    rate1: float,
    T1_calendar_days: int,
    rate2: float,
    T2_calendar_days: int,
    target_calendar_days: int = 30,
    debug: bool = False,
) -> tuple[float, IvProvenance]:
    """Constant-maturity VIX-style σ + ``IvProvenance``.

    Same math as ``vix_style_iv30`` plus a combined provenance object. The
    two per-expiry provenance values are combined by their variance-time
    contribution to the final IV30 — the same weights used to produce the
    final σ — so the metric the consumer sees corresponds to what actually
    drives the number it sees.

    The combined ``strike_coverage_score`` is the *minimum* of the two
    expiries' scores: a chain with bad wings on either expiry compromises
    the IV30 even if the other expiry is well-covered.
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

    rep1, prov1 = replicate_expiry_variance_with_provenance(
        expiry1_quotes, rate=rate1, T_years=T1, debug=debug
    )
    rep2, prov2 = replicate_expiry_variance_with_provenance(
        expiry2_quotes, rate=rate2, T_years=T2, debug=debug
    )

    w = (T2 - T_target) / (T2 - T1)
    var_target_times_T = w * rep1.sigma_squared_T * T1 + (1.0 - w) * rep2.sigma_squared_T * T2
    sigma_squared = var_target_times_T / T_target
    if sigma_squared <= 0:
        raise ValueError(f"replicated variance non-positive: {sigma_squared}")
    sigma_target = math.sqrt(sigma_squared)

    # Combine per-expiry provenance using each expiry's variance-time share
    # of the final IV30. Weights sum to 1.0 by construction.
    weight1 = (w * rep1.sigma_squared_T * T1) / var_target_times_T
    weight2 = ((1.0 - w) * rep2.sigma_squared_T * T2) / var_target_times_T

    combined_vcs = (
        weight1 * prov1.variance_contribution_synthetic
        + weight2 * prov2.variance_contribution_synthetic
    )
    combined_vcs = max(0.0, min(1.0, combined_vcs))

    combined_mix: dict[PriceSource, float] = {}
    for src, share in prov1.price_source_mix.items():
        combined_mix[src] = combined_mix.get(src, 0.0) + weight1 * share
    for src, share in prov2.price_source_mix.items():
        combined_mix[src] = combined_mix.get(src, 0.0) + weight2 * share
    total = sum(combined_mix.values())
    if total > 0:
        combined_mix = {src: v / total for src, v in combined_mix.items()}

    combined_coverage = min(prov1.strike_coverage_score, prov2.strike_coverage_score)

    combined_contribs: list[dict] | None = None
    if debug:
        combined_contribs = []
        if prov1.per_strike_contributions:
            for c in prov1.per_strike_contributions:
                combined_contribs.append({**c, "expiry_calendar_days": T1_calendar_days})
        if prov2.per_strike_contributions:
            for c in prov2.per_strike_contributions:
                combined_contribs.append({**c, "expiry_calendar_days": T2_calendar_days})

    combined_prov = IvProvenance(
        iv_source="internal_solver",
        price_source_mix=combined_mix,
        variance_contribution_synthetic=combined_vcs,
        strike_coverage_score=combined_coverage,
        per_strike_contributions=combined_contribs,
    )

    return sigma_target, combined_prov
