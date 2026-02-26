"""Feature registry with metadata for documentation and UI display."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FeatureName(str, Enum):
    """Supported Phase 1 features."""

    MOMENTUM_5M = "momentum_5m"
    RSI_14 = "rsi_14"
    REALIZED_VOL_30 = "realized_vol_30"
    VOLUME_ZSCORE = "volume_zscore"
    MACD_SIGNAL = "macd_signal"


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
    category: str  # "momentum" | "volatility" | "volume"


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
