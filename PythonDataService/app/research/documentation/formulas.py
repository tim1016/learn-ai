"""Mathematical documentation for all research features and validation tests.

Every LaTeX string, variable definition, worked example, and interpretation
is stored here so the Angular UI can display them transparently.
No black-box computations allowed.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Target definition
# ---------------------------------------------------------------------------
TARGET_DOCUMENTATION = {
    "name": "15-Minute Forward Log Return",
    "formula_latex": r"R_{15}(t) = \ln\!\left(\frac{P_{t+15}}{P_t}\right)",
    "variables": {
        "P_t": "Closing price at bar t",
        "P_{t+15}": "Closing price 15 bars ahead",
        "ln": "Natural logarithm",
    },
    "example": {
        "inputs": {"P_t": 100.0, "P_{t+15}": 101.0},
        "calculation": "ln(101/100) = ln(1.01) = 0.00995",
        "result": 0.00995,
    },
    "interpretation": (
        "Positive value = price increased over next 15 minutes. "
        "Negative value = price decreased. "
        "Log returns are additive across time and approximately symmetric."
    ),
    "constraints": [
        "No cross-day contamination: returns that span midnight are set to NaN",
        "Last 15 bars of each trading day have NaN target",
    ],
}

# ---------------------------------------------------------------------------
# Feature formulas
# ---------------------------------------------------------------------------
FEATURE_DOCUMENTATION = {
    "momentum_5m": {
        "name": "5-Minute Momentum",
        "formula_latex": r"\text{MOM}_5(t) = \frac{P_t - P_{t-5}}{P_{t-5}}",
        "variables": {
            "P_t": "Close price at current bar",
            "P_{t-5}": "Close price 5 bars ago",
        },
        "example": {
            "inputs": {"P_t": 101.0, "P_{t-5}": 100.0},
            "calculation": "(101 - 100) / 100 = 0.01",
            "result": 0.01,
        },
        "interpretation": "Positive = short-term upward momentum; Negative = downward.",
        "implementation": "pandas pct_change(periods=5)",
    },
    "rsi_14": {
        "name": "RSI (14-period)",
        "formula_latex": r"\text{RSI} = 100 - \frac{100}{1 + RS}, \quad RS = \frac{\overline{\text{gain}}_{14}}{\overline{\text{loss}}_{14}}",
        "variables": {
            "RS": "Relative Strength = avg gain / avg loss over 14 bars",
            "gain": "max(0, close_t - close_{t-1})",
            "loss": "max(0, close_{t-1} - close_t)",
        },
        "example": {
            "inputs": {"avg_gain": 0.5, "avg_loss": 0.3},
            "calculation": "RS = 0.5/0.3 = 1.667, RSI = 100 - 100/(1+1.667) = 62.5",
            "result": 62.5,
        },
        "interpretation": ">70 overbought (potential sell); <30 oversold (potential buy).",
        "implementation": "pandas-ta rsi(length=14)",
    },
    "realized_vol_30": {
        "name": "Realized Volatility (30-bar)",
        "formula_latex": r"\sigma_{30}(t) = \text{std}\!\left(\ln\frac{P_i}{P_{i-1}}\right)_{i=t-29}^{t}",
        "variables": {
            "P_i": "Close price at bar i",
            "std": "Sample standard deviation",
            "ln": "Natural logarithm",
        },
        "example": {
            "inputs": {"log_returns_std": 0.0012},
            "calculation": "std of 30 consecutive log returns = 0.0012",
            "result": 0.0012,
        },
        "interpretation": "Higher = more uncertainty; lower = calmer price action.",
        "implementation": "log(close/close.shift(1)).rolling(30).std()",
    },
    "volume_zscore": {
        "name": "Volume Z-Score",
        "formula_latex": r"z(t) = \frac{V_t - \mu_{30}}{\sigma_{30}}",
        "variables": {
            "V_t": "Volume at bar t",
            "mu_30": "Rolling 30-bar mean volume",
            "sigma_30": "Rolling 30-bar std of volume",
        },
        "example": {
            "inputs": {"V_t": 1_500_000, "mu_30": 1_000_000, "sigma_30": 200_000},
            "calculation": "(1500000 - 1000000) / 200000 = 2.5",
            "result": 2.5,
        },
        "interpretation": "|z| > 2 = unusual volume activity; may precede large moves.",
        "implementation": "(volume - rolling_mean) / rolling_std, window=30",
    },
    "macd_signal": {
        "name": "MACD Signal Line",
        "formula_latex": r"\text{Signal} = \text{EMA}_9\!\left(\text{EMA}_{12}(P) - \text{EMA}_{26}(P)\right)",
        "variables": {
            "EMA_k": "Exponential moving average with span k",
            "P": "Close prices",
            "MACD": "EMA_12 - EMA_26",
        },
        "example": {
            "inputs": {"EMA_12": 101.5, "EMA_26": 101.0},
            "calculation": "MACD = 0.5; Signal = EMA_9(MACD series)",
            "result": 0.45,
        },
        "interpretation": "MACD crossing above signal = bullish; below = bearish.",
        "implementation": "pandas-ta macd(fast=12, slow=26, signal=9), signal column",
    },
}

# ---------------------------------------------------------------------------
# Validation test formulas
# ---------------------------------------------------------------------------
VALIDATION_DOCUMENTATION = {
    "information_coefficient": {
        "name": "Information Coefficient (IC)",
        "formula_latex": r"IC_d = \text{Spearman}\!\left(\text{rank}(F_d),\, \text{rank}(R_d)\right)",
        "additional_formulas": {
            "mean_ic": r"\overline{IC} = \frac{1}{N}\sum_{d=1}^{N} IC_d",
            "t_stat": r"t = \frac{\overline{IC}}{\sigma_{IC} / \sqrt{N}}",
        },
        "variables": {
            "F_d": "Feature values on day d",
            "R_d": "Forward returns on day d",
            "N": "Number of trading days",
            "sigma_IC": "Standard deviation of daily ICs",
        },
        "interpretation": (
            "IC > 0.02 with |t| > 2 suggests meaningful predictive signal. "
            "Most features will have IC near zero."
        ),
    },
    "adf_test": {
        "name": "Augmented Dickey-Fuller (ADF) Test",
        "formula_latex": r"H_0: \text{unit root exists (non-stationary)}",
        "variables": {
            "p-value": "Probability under H0",
            "threshold": "0.05 (reject H0 if p < 0.05)",
        },
        "interpretation": (
            "p < 0.05 → reject H0 → feature is likely stationary. "
            "Non-stationary features have time-varying distributions and are unreliable."
        ),
    },
    "kpss_test": {
        "name": "KPSS Stationarity Test",
        "formula_latex": r"H_0: \text{series is stationary}",
        "variables": {
            "p-value": "Probability under H0",
            "threshold": "0.05 (fail to reject if p > 0.05)",
        },
        "interpretation": (
            "p > 0.05 → fail to reject H0 → consistent with stationarity. "
            "Combined with ADF: stationary if ADF rejects AND KPSS does not reject."
        ),
    },
    "quantile_analysis": {
        "name": "Quantile Monotonicity Check",
        "formula_latex": r"E[R_{15} \mid Q_i] \text{ should increase with } i",
        "variables": {
            "Q_i": "i-th quantile bucket of the feature (i = 1..5)",
            "E[R|Q_i]": "Mean forward return in bucket i",
        },
        "interpretation": (
            "If higher feature values predict higher returns (monotonically), "
            "the feature has directional predictive power."
        ),
    },
}


def get_all_documentation() -> dict:
    """Return complete documentation bundle for the Angular UI."""
    return {
        "target": TARGET_DOCUMENTATION,
        "features": FEATURE_DOCUMENTATION,
        "validation": VALIDATION_DOCUMENTATION,
    }
