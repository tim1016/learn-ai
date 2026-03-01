"""Feature registry with metadata for documentation and UI display."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FeatureName(str, Enum):
    """Supported features."""

    MOMENTUM_5M = "momentum_5m"
    RSI_14 = "rsi_14"
    REALIZED_VOL_30 = "realized_vol_30"
    VOLUME_ZSCORE = "volume_zscore"
    MACD_SIGNAL = "macd_signal"

    # Options-derived features (daily frequency)
    IV_30D = "iv_30d"
    IV_RANK_60 = "iv_rank_60"
    LOG_SKEW = "log_skew"
    IV_RANK_252 = "iv_rank_252"
    VRP_5 = "vrp_5"


@dataclass(frozen=True)
class FeatureMetadata:
    """Documentation and metadata for a single feature."""

    name: str
    formula_latex: str
    variables: str
    example: str
    interpretation: str
    implementation_note: str
    window: int
    category: str  # "momentum" | "volatility" | "volume" | "options"
    data_source: str = "stock"  # "stock" | "options"


FEATURE_REGISTRY: dict[FeatureName, FeatureMetadata] = {
    FeatureName.MOMENTUM_5M: FeatureMetadata(
        name="5-Minute Momentum",
        formula_latex=r"\text{MOM}_{5}(t) = \frac{P_t - P_{t-5}}{P_{t-5}}",
        variables=r"P_t = \text{close at bar } t; \; 5 = \text{lookback bars}",
        example="If P_t = 101, P_{t-5} = 100 then MOM_5 = 0.01 (+1%)",
        interpretation="Positive = short-term upward momentum; Negative = downward momentum.",
        implementation_note="Uses pandas pct_change with period=5.",
        window=5,
        category="momentum",
    ),
    FeatureName.RSI_14: FeatureMetadata(
        name="RSI (14-period)",
        formula_latex=r"\text{RSI} = 100 - \frac{100}{1 + RS}, \quad RS = \frac{\text{avg\_gain}_{14}}{\text{avg\_loss}_{14}}",
        variables=r"\text{avg\_gain} = \text{mean of positive price changes over 14 bars}; \; \text{avg\_loss} = \text{mean of negative price changes}",
        example="If avg_gain = 0.5, avg_loss = 0.3 then RS = 1.67, RSI = 62.5",
        interpretation="0-100 scale; >70 overbought (sell signal); <30 oversold (buy signal).",
        implementation_note="Uses pandas-ta rsi() with length=14.",
        window=14,
        category="momentum",
    ),
    FeatureName.REALIZED_VOL_30: FeatureMetadata(
        name="Realized Volatility (30-bar)",
        formula_latex=r"\sigma_{30}(t) = \text{std}\left(\ln\frac{P_i}{P_{i-1}}\right)_{i=t-29}^{t}",
        variables=r"P_i = \text{close at bar } i; \; \text{std} = \text{sample standard deviation over 30 bars}",
        example="If log returns std over 30 bars = 0.0012, realized_vol_30 = 0.0012",
        interpretation="Higher values = more price uncertainty / risk; Lower = calmer market.",
        implementation_note="Computes log returns then rolling std with window=30.",
        window=30,
        category="volatility",
    ),
    FeatureName.VOLUME_ZSCORE: FeatureMetadata(
        name="Volume Z-Score",
        formula_latex=r"z(t) = \frac{V_t - \mu_{30}}{\sigma_{30}}",
        variables=r"V_t = \text{volume at bar } t; \; \mu_{30}, \sigma_{30} = \text{rolling mean and std of volume over 30 bars}",
        example="If V_t = 1.5M, mu_30 = 1.0M, sigma_30 = 0.2M then z = 2.5",
        interpretation="|z| > 2 indicates unusual volume activity (potential signal).",
        implementation_note="Uses pandas rolling(30).mean() and rolling(30).std().",
        window=30,
        category="volume",
    ),
    FeatureName.MACD_SIGNAL: FeatureMetadata(
        name="MACD Signal Line",
        formula_latex=r"\text{Signal} = \text{EMA}_9\!\left(\text{EMA}_{12}(P) - \text{EMA}_{26}(P)\right)",
        variables=r"\text{EMA}_k = \text{exponential moving average with span } k; \; P = \text{close prices}",
        example="When MACD crosses above Signal line, bullish momentum shift",
        interpretation="Signal line crossovers indicate momentum direction changes.",
        implementation_note="Uses pandas-ta macd(fast=12, slow=26, signal=9); returns signal column.",
        window=26,
        category="momentum",
    ),
    # Options-derived features
    FeatureName.IV_30D: FeatureMetadata(
        name="30-Day ATM Implied Volatility",
        formula_latex=r"\text{IV}_{30d}(t) = w_1 \cdot \text{IV}_{\text{low}} + w_2 \cdot \text{IV}_{\text{high}}",
        variables=r"w_1 = \frac{DTE_H - 30}{DTE_H - DTE_L}; \; w_2 = \frac{30 - DTE_L}{DTE_H - DTE_L}; \; \text{30-day constant-maturity interpolation}",
        example="If IV_low (DTE=25) = 0.20, IV_high (DTE=35) = 0.22, then IV_30d = 0.21",
        interpretation="Market's expectation of annualized volatility over the next 30 days. Direct output from IV derivation engine.",
        implementation_note="Derived from expired options via Black-Scholes inversion with 30-day constant-maturity interpolation. European BS approximation — acceptable for relative IV movements.",
        window=0,
        category="options",
        data_source="options",
    ),
    FeatureName.IV_RANK_60: FeatureMetadata(
        name="IV Rank (60-Day)",
        formula_latex=r"\text{IVR}_{60}(t) = \frac{\text{IV}_t - \min(\text{IV}_{60})}{\max(\text{IV}_{60}) - \min(\text{IV}_{60})}",
        variables=r"\text{IV}_t = \text{30-day ATM IV at time } t; \; \text{IV}_{60} = \text{IV values over prior 60 trading days}",
        example="If IV_t = 0.25, min_60 = 0.15, max_60 = 0.35, then IVR = 0.50",
        interpretation="0-1 scale. High IVR = IV is elevated vs recent history (mean-reversion candidate). Low = IV is depressed.",
        implementation_note="60-day rolling min/max rank. min_periods=30 for warm-up. Start with 60-day window before validating with 252-day.",
        window=60,
        category="options",
        data_source="options",
    ),
    FeatureName.LOG_SKEW: FeatureMetadata(
        name="Log Put-Call Skew",
        formula_latex=r"\text{Skew}(t) = \ln\left(\frac{\text{IV}_{30d,\text{put}}(t)}{\text{IV}_{30d,\text{call}}(t)}\right)",
        variables=r"\text{IV}_{30d,\text{put}} = \text{OTM put IV (5\% below ATM)}; \; \text{IV}_{30d,\text{call}} = \text{OTM call IV (5\% above ATM)}",
        example="If IV_put = 0.28, IV_call = 0.22, then log_skew = ln(1.27) = 0.24",
        interpretation="Positive = elevated put demand (downside protection). Scale-invariant across vol regimes. More statistically normal than simple difference.",
        implementation_note="Log-transform (not difference) for scale invariance. Both legs require volume >= 50 and OI >= 100.",
        window=0,
        category="options",
        data_source="options",
    ),
    FeatureName.IV_RANK_252: FeatureMetadata(
        name="IV Rank (252-Day / Annual)",
        formula_latex=r"\text{IVR}_{252}(t) = \frac{\text{IV}_t - \min(\text{IV}_{252})}{\max(\text{IV}_{252}) - \min(\text{IV}_{252})}",
        variables=r"\text{IV}_{252} = \text{IV values over prior 252 trading days (1 year)}",
        example="If IV_t = 0.30, min_252 = 0.12, max_252 = 0.45, then IVR = 0.545",
        interpretation="Full-year IV rank. More stable than 60-day. Requires validated IV data (Cycle 2 feature).",
        implementation_note="252-day rolling min/max rank. min_periods=60. Deferred to Cycle 2 after IV data quality confirmed.",
        window=252,
        category="options",
        data_source="options",
    ),
    FeatureName.VRP_5: FeatureMetadata(
        name="Volatility Risk Premium (5-Day)",
        formula_latex=r"\text{VRP}(t) = \text{IV}_{30d}(t) - \text{RV}_{5}(t)",
        variables=r"\text{RV}_5 = \sqrt{252} \cdot \text{std}(\ln(P_i / P_{i-1}))_{i=t-4}^{t}; \; \text{annualized 5-day realized vol}",
        example="If IV_30d = 0.25, RV_5 = 0.18, then VRP = 0.07 (options are 'expensive')",
        interpretation="Positive VRP = options overpriced vs realized. Signal mode uses trailing RV (no lookahead). Research mode uses forward RV.",
        implementation_note="Research: VRP = IV - RV_forward_5d. Signal: VRP = IV - RV_trailing_5d. Deferred to Cycle 2.",
        window=5,
        category="options",
        data_source="options",
    ),
}


# Features that use options IV data (not stock OHLCV)
OPTIONS_FEATURES = {
    FeatureName.IV_30D.value,
    FeatureName.IV_RANK_60.value,
    FeatureName.LOG_SKEW.value,
    FeatureName.IV_RANK_252.value,
    FeatureName.VRP_5.value,
}


def get_feature_metadata(feature_name: str) -> FeatureMetadata | None:
    """Look up feature metadata by name string."""
    try:
        key = FeatureName(feature_name)
        return FEATURE_REGISTRY.get(key)
    except ValueError:
        return None


def list_available_features() -> list[str]:
    """Return all registered feature name strings."""
    return [f.value for f in FeatureName]
