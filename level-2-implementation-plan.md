# Level 2 — Feature Engineering: Detailed Implementation Plan

## Executive Summary

Transform the ML pipeline from raw OHLCV (8 features) to a rich feature set (50+ engineered features) using **pandas-ta** for established indicators and manual computation for specialized volatility estimators. All features are computable from Polygon.io OHLCV data — no additional subscriptions required.

---

## Data Source Assessment: What Polygon.io Can & Cannot Provide

### Available (Stocks Starter Plan — $29/mo)

| Data | Polygon Endpoint | ML Use |
|------|-----------------|--------|
| OHLCV bars (daily/minute/hour) | `GET /v2/aggs/ticker/{}/range/...` | Core input for all features |
| Volume + VWAP + transaction count | Included in aggregates response | Volume & microstructure features |
| Options chain snapshot (Greeks, IV, OI) | `GET /v3/snapshot/options/{underlyingAsset}` | Current IV only (not historical) |
| Multi-ticker snapshots (SPY, sector ETFs) | `GET /v2/snapshot/locale/us/markets/stocks/tickers` | Cross-asset signals |
| Market holidays | `GET /v1/marketstatus/upcoming` | Calendar features |
| Technical indicators via API | `GET /v1/indicators/sma/...` etc. | Redundant — we compute locally |

### NOT Available (Would Need Additional Subscription or Provider)

| Data | Why Not | Workaround |
|------|---------|------------|
| **VIX index** | Requires Polygon Indices plan ($49/mo extra) | Use realized vol + ATR + Bollinger width as volatility proxies |
| **Treasury yields (10Y, 2Y)** | Not on Polygon at all | Skip — not critical for single-stock prediction |
| **Historical options IV time series** | Polygon only provides current snapshots, not historical IV | Skip — would need daily snapshot collection over time |
| **Sector rotation indices** | Not available as indices | Approximate via sector ETF returns (XLK, XLF, XLE, etc.) |
| **Fundamental data time series** | Only current snapshot (market cap, etc.) | Skip — use as static feature if needed later |

### Decision: All Level 2 features will be computed locally from OHLCV data

We won't fetch technical indicators from Polygon's API — instead we compute everything locally using **pandas-ta** and manual formulas. This is faster (no API calls), more flexible, and avoids rate limits.

Cross-asset signals (SPY correlation) require one additional `fetch_aggregates()` call per reference ticker — this is a minor API cost.

---

## Feature Categories & Exact Specifications

### Category 1: Technical Indicators (via `pandas-ta`)

All available out-of-the-box in pandas-ta. No custom implementation needed.

| Feature Name | pandas-ta Function | Parameters | Output Columns | Why |
|-------------|-------------------|------------|----------------|-----|
| RSI | `ta.rsi(close, length=14)` | length=14 | `rsi_14` | Overbought/oversold boundaries — XGBoost splits on thresholds naturally |
| MACD | `ta.macd(close, fast=12, slow=26, signal=9)` | fast=12, slow=26, signal=9 | `macd_value`, `macd_signal`, `macd_hist` | Momentum crossover signals; histogram shows acceleration |
| Bollinger Bands | `ta.bbands(close, length=20, std=2)` | length=20, std=2.0 | `bb_width`, `bb_pctb` | Width = volatility proxy; %B = position within bands |
| ATR | `ta.atr(high, low, close, length=14)` | length=14 | `atr_14` | Volatility measure; used in stop-loss calculations |
| ADX | `ta.adx(high, low, close, length=14)` | length=14 | `adx_14`, `dmp_14`, `dmn_14` | Trend strength (ADX > 25 = trending); DM+/DM- = direction |
| Stochastic | `ta.stoch(high, low, close, k=14, d=3)` | k=14, d=3 | `stoch_k`, `stoch_d` | Momentum oscillator; %K/%D crossovers |
| Williams %R | `ta.willr(high, low, close, length=14)` | length=14 | `willr_14` | Similar to stochastic but inverted scale (-100 to 0) |
| CCI | `ta.cci(high, low, close, length=20)` | length=20 | `cci_20` | Cyclical indicator; extreme values signal reversals |
| OBV | `ta.obv(close, volume)` | none | `obv` | Volume-weighted price direction confirmation |
| CMF | `ta.cmf(high, low, close, volume, length=20)` | length=20 | `cmf_20` | Buying/selling pressure based on close position within range |
| MFI | `ta.mfi(high, low, close, volume, length=14)` | length=14 | `mfi_14` | Volume-weighted RSI; overbought/oversold with volume confirmation |
| NATR | `ta.natr(high, low, close, length=14)` | length=14 | `natr_14` | Normalized ATR (% of close) — comparable across price levels |

**Total: 17 features from 12 indicator functions**

### Category 2: Volatility Estimators (Manual Implementation)

Garman-Klass and Parkinson are **NOT in pandas-ta**. These are range-based volatility estimators that are more efficient than close-to-close variance. Formulas are well-established (see references).

| Feature Name | Formula | Rolling Window | Output Column | Why |
|-------------|---------|----------------|---------------|-----|
| Close-to-Close Vol | `rolling_std(log_return, window)` | 5, 10, 20 | `cc_vol_5`, `cc_vol_10`, `cc_vol_20` | Standard historical volatility at multiple horizons |
| Parkinson Vol | `sqrt((1/4n*ln2) * Σ(ln(H/L))²)` | 20 | `parkinson_vol_20` | 5x more efficient than close-to-close; uses high-low range |
| Garman-Klass Vol | `sqrt((1/n) * Σ[0.5*(ln(H/L))² - (2ln2-1)*(ln(C/O))²])` | 20 | `garman_klass_vol_20` | 7.4x more efficient; uses OHLC |
| Rogers-Satchell Vol | `sqrt((1/n) * Σ[ln(H/C)*ln(H/O) + ln(L/C)*ln(L/O)])` | 20 | `rogers_satchell_vol_20` | Handles drift (trending markets) — more robust than GK |
| Vol Ratio | `cc_vol_5 / cc_vol_20` | derived | `vol_ratio_5_20` | Volatility regime change detector (>1 = expanding vol) |

**Total: 7 features** (3 close-to-close at different windows + 3 range-based + 1 ratio)

**Implementation reference**: Formulas from Yang & Zhang (2000), available in `mlfinlab` and `volatility-trading` libraries — but we implement manually (4 simple formulas, no extra dependency needed).

### Category 3: Lagged Features & Rolling Statistics (Manual, pandas)

These are the **highest importance features** per XGBoost financial ML research (NEPSE 2025 study: rolling mean ranked #1, RSI #2, lag_1 #3).

| Feature Name | Computation | Output Columns | Why |
|-------------|-------------|----------------|-----|
| Lagged log returns | `log_return.shift(n)` for n in [1,2,3,5,10,21] | `lag_ret_1` through `lag_ret_21` | Autoregressive signal; lag_1 consistently top feature |
| Rolling mean of returns | `log_return.rolling(w).mean()` for w in [5, 10, 20] | `roll_mean_5`, `roll_mean_10`, `roll_mean_20` | Short/medium/long term trend; 10-day ranked #1 in studies |
| Rolling std of returns | `log_return.rolling(w).std()` for w in [5, 10, 20] | `roll_std_5`, `roll_std_10`, `roll_std_20` | Same as cc_vol but on returns — captures regime |
| Rolling skewness | `log_return.rolling(20).skew()` | `roll_skew_20` | Asymmetry in return distribution — tail risk indicator |
| Rolling kurtosis | `log_return.rolling(20).kurt()` | `roll_kurt_20` | Fat tails — extreme move probability |
| Return momentum | `close.pct_change(n)` for n in [5, 10, 21] | `mom_5`, `mom_10`, `mom_21` | Multi-period momentum (different from daily return) |
| Mean reversion | `(close - SMA(20)) / close` | `mean_rev_20` | Distance from moving average — mean reversion signal |

**Total: 18 features** (6 lags + 3 rolling means + 3 rolling stds + 2 higher moments + 3 momentum + 1 mean reversion)

### Category 4: Volume Features (pandas-ta + manual)

| Feature Name | Computation | Output Column | Why |
|-------------|-------------|---------------|-----|
| Volume ratio | `volume / volume.rolling(20).mean()` | `vol_ratio` | Relative volume — unusual volume signals events |
| Volume momentum | `volume.pct_change(5)` | `vol_mom_5` | Volume trend |
| VWAP distance | `(close - vwap) / close` | `vwap_dist` | Price position relative to volume-weighted average |
| OBV (from Category 1) | `ta.obv(close, volume)` | `obv` | Already counted above |
| CMF (from Category 1) | `ta.cmf(...)` | `cmf_20` | Already counted above |
| MFI (from Category 1) | `ta.mfi(...)` | `mfi_14` | Already counted above |
| OBV slope | `obv.diff(5) / 5` | `obv_slope_5` | OBV direction — divergence from price = signal |

**Total: 4 new features** (OBV, CMF, MFI already counted in Category 1)

### Category 5: Calendar Features (manual, pandas)

| Feature Name | Computation | Output Column | Encoding | Why |
|-------------|-------------|---------------|----------|-----|
| Day of week | `timestamp.dt.dayofweek` | `dow_sin`, `dow_cos` | Cyclical (sin/cos) | Monday/Friday effects are documented anomalies |
| Month | `timestamp.dt.month` | `month_sin`, `month_cos` | Cyclical (sin/cos) | January effect, sell-in-May seasonality |
| Week of year | `timestamp.dt.isocalendar().week` | `woy_sin`, `woy_cos` | Cyclical (sin/cos) | Annual seasonality patterns |
| Is month end | `timestamp.dt.is_month_end` | `is_month_end` | Binary (0/1) | Window dressing, rebalancing effects |
| Is quarter end | `BQuarterEnd` check | `is_quarter_end` | Binary (0/1) | Quarterly rebalancing, earnings clustering |
| Days since holiday | Computed from Polygon market holidays API | `days_since_holiday` | Integer | Post-holiday drift effect |

**Cyclical encoding**: Day of week 0-4 would be treated as ordinal by XGBoost (Friday=4 > Monday=0, which is meaningless). Sin/cos encoding preserves cyclical nature: `sin(2π * day/5), cos(2π * day/5)`.

**Total: 9 features** (3 cyclical pairs + 2 binary + 1 integer)

### Category 6: Cross-Asset Signals (Polygon multi-ticker fetch)

Requires fetching SPY daily data for the same date range — one additional `fetch_aggregates()` call.

| Feature Name | Computation | Output Column | Why |
|-------------|-------------|---------------|-----|
| SPY daily return | `spy_close.pct_change()` | `spy_return` | Market direction — most stocks move with the market |
| Relative strength vs SPY | `ticker_return - spy_return` | `relative_strength` | Stock-specific alpha signal (outperforming/underperforming market) |
| Rolling correlation with SPY | `ticker_return.rolling(20).corr(spy_return)` | `spy_corr_20` | Beta proxy — high correlation = market-driven; low = idiosyncratic |
| SPY rolling volatility | `spy_return.rolling(20).std()` | `spy_vol_20` | Market volatility regime (VIX proxy — see note below) |

**VIX Proxy Strategy**: Since VIX requires an extra $49/mo Polygon subscription, we approximate market fear using:
1. `spy_vol_20` — 20-day realized volatility of SPY (correlates ~0.8 with VIX)
2. `bb_width` of the target ticker — individual stock's volatility expansion
3. `vol_ratio_5_20` — short-term vs long-term vol ratio (vol regime change)

These three together capture most of what VIX provides for single-stock prediction.

**Total: 4 features**

### Category 7: Microstructure Proxies (from OHLCV)

| Feature Name | Computation | Output Column | Why |
|-------------|-------------|---------------|-----|
| High-low spread | `(high - low) / close` | `hl_spread` | Bid-ask spread proxy — wider in volatile/illiquid periods |
| Close position | `(close - low) / (high - low)` | `close_position` | Where price closed within day's range (1.0 = at high, 0 = at low) |

**Total: 2 features**

---

## Feature Summary

| Category | Features | Library | Data Source |
|----------|----------|---------|-------------|
| Technical Indicators | 17 | pandas-ta | OHLCV (already fetched) |
| Volatility Estimators | 7 | Manual (4 formulas) | OHLCV (already fetched) |
| Lag & Rolling Stats | 18 | pandas (shift, rolling) | Computed from returns |
| Volume Features | 4 | pandas-ta + manual | OHLCV (already fetched) |
| Calendar Features | 9 | pandas + Polygon holidays API | Timestamps + 1 API call |
| Cross-Asset (SPY) | 4 | pandas | 1 extra `fetch_aggregates()` for SPY |
| Microstructure | 2 | Manual | OHLCV (already fetched) |
| **TOTAL** | **61** | | |

**Warm-up period**: The longest lookback is 26 bars (MACD slow=26). With rolling windows of 20 on top of that, we need ~50 bars of warm-up data. We'll fetch 50 extra bars before the requested start date and drop them after feature computation.

---

## What We're NOT Building (and Why)

| Feature | Reason for Exclusion | Future Option |
|---------|---------------------|---------------|
| VIX / volatility index | Requires $49/mo Indices subscription | SPY vol + ATR + BB width are adequate proxies |
| Treasury yields (10Y, 2Y spread) | Not available on Polygon at any tier | Add FRED API integration in Level 3+ if macro signals wanted |
| Historical IV time series | Polygon only has current options snapshots | Would need daily snapshot collection service running over weeks |
| Sentiment (news, social) | Not available from Polygon | Would need separate NLP pipeline + data source |
| Order book / Level 2 | Not available on Starter plan | Not useful for daily-frequency prediction anyway |
| Intraday patterns | LSTM could use minute bars, but adds enormous complexity | Defer to Level 4 multi-resolution architecture |

---

## Implementation Architecture

### New Module: `PythonDataService/app/ml/features/`

```
app/ml/features/
├── __init__.py
├── engine.py              # FeatureEngine: orchestrates all feature groups
├── technical.py           # pandas-ta wrapper: RSI, MACD, BBands, ATR, ADX, etc.
├── volatility.py          # Garman-Klass, Parkinson, Rogers-Satchell, close-to-close
├── lag_features.py        # Lagged returns, rolling stats, momentum
├── volume_features.py     # Volume ratio, VWAP distance, OBV slope
├── calendar_features.py   # Day of week, month, holidays (cyclical encoding)
├── cross_asset.py         # SPY returns, relative strength, correlation
├── microstructure.py      # High-low spread, close position
└── config.py              # FeatureGroupConfig: which groups to enable/disable
```

### FeatureEngine API Design

```python
from dataclasses import dataclass

@dataclass
class FeatureGroupConfig:
    """Controls which feature groups are computed."""
    technical: bool = True          # RSI, MACD, BBands, ATR, ADX, Stochastic, etc.
    volatility: bool = True         # GK, Parkinson, RS, close-to-close vol
    lag_features: bool = True       # Lagged returns, rolling stats, momentum
    volume: bool = True             # Volume ratio, VWAP dist, OBV slope
    calendar: bool = True           # Day of week, month, holidays
    cross_asset: bool = False       # SPY correlation (requires extra API call)
    microstructure: bool = True     # HL spread, close position

    # Customization
    lag_periods: list[int] = field(default_factory=lambda: [1, 2, 3, 5, 10, 21])
    rolling_windows: list[int] = field(default_factory=lambda: [5, 10, 20])
    rsi_length: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_length: int = 20
    atr_length: int = 14
    adx_length: int = 14
    stoch_k: int = 14
    stoch_d: int = 3
    vol_window: int = 20


class FeatureEngine:
    """Computes all feature groups from OHLCV DataFrame."""

    def __init__(self, config: FeatureGroupConfig, provider: MarketDataProvider | None = None):
        self.config = config
        self.provider = provider  # Needed for cross-asset data fetching

    def compute(self, df: pd.DataFrame, ticker: str | None = None) -> pd.DataFrame:
        """
        Input:  DataFrame with columns [timestamp, open, high, low, close, volume, vwap]
        Output: DataFrame with all original columns + engineered features
                NaN rows from warm-up period are dropped
        """
        ...

    def get_feature_names(self) -> list[str]:
        """Returns list of all feature column names that will be generated."""
        ...
```

### Integration with Existing Pipeline

Current flow in `pipeline.py`:
```
fetch OHLCV → compute returns/log_return → shift features → winsorize → split → scale → window
```

New flow:
```
fetch OHLCV → FeatureEngine.compute(df) → shift features → winsorize → split → scale → window/tabular
                    ↑
           Computes all 61 features,
           drops NaN warm-up rows,
           returns enriched DataFrame
```

**Key integration point**: `FeatureEngine.compute()` is called BEFORE the existing shift/scale/split steps. The engine adds columns to the DataFrame; downstream code treats them like any other feature.

### Modified Files

| File | Change |
|------|--------|
| `app/ml/preprocessing/pipeline.py` | Insert `FeatureEngine.compute()` call after OHLCV fetch, before shift/split. Update feature list handling. |
| `app/ml/models/schemas.py` | Add `FeatureGroupConfig` to `TrainingConfig`. Update feature validation to accept engineered feature names. |
| `app/ml/models/api_schemas.py` | Add `feature_groups` field to `TrainRequest` and `ValidateRequest`. |
| `app/ml/providers/polygon_provider.py` | Add method to fetch SPY data for cross-asset features. |
| `app/ml/providers/mock_provider.py` | Update mock to generate synthetic data with all OHLCV columns needed. |

---

## Implementation Phases

### Phase 2.1: Core Feature Module (Technical + Volatility)

**Goal**: Build the feature computation engine with the two most impactful categories.

**Files to create**:
- `app/ml/features/__init__.py`
- `app/ml/features/config.py` — `FeatureGroupConfig` dataclass
- `app/ml/features/technical.py` — All pandas-ta indicators
- `app/ml/features/volatility.py` — GK, Parkinson, RS, close-to-close vol
- `app/ml/features/engine.py` — `FeatureEngine` orchestrator (partial — tech + vol only)

**Tests**:
- `tests/test_features_technical.py` — Verify each indicator produces correct column names, correct value ranges (RSI 0-100, etc.), handles NaN warm-up correctly
- `tests/test_features_volatility.py` — Verify GK/Parkinson/RS against known values from reference implementations

**Acceptance criteria**:
- Given a 500-row OHLCV DataFrame, `FeatureEngine.compute()` returns DataFrame with ~24 new columns
- No NaN values in output (warm-up rows dropped)
- RSI values between 0-100, ATR > 0, ADX between 0-100
- Volatility estimators produce positive values

### Phase 2.2: Lag Features & Rolling Statistics

**Files to create**:
- `app/ml/features/lag_features.py` — Lagged returns + rolling stats + momentum

**Tests**:
- `tests/test_features_lag.py` — Verify lag alignment (lag_1 at row t should equal return at row t-1), rolling window sizes, no look-ahead

**Acceptance criteria**:
- Lag features are correctly aligned (no off-by-one)
- Rolling statistics match pandas rolling output
- Higher moments (skew, kurtosis) are numerically stable

### Phase 2.3: Volume, Calendar, Microstructure

**Files to create**:
- `app/ml/features/volume_features.py`
- `app/ml/features/calendar_features.py`
- `app/ml/features/microstructure.py`

**Tests**:
- `tests/test_features_volume.py` — Volume ratio > 0, OBV slope direction matches
- `tests/test_features_calendar.py` — Cyclical encoding produces sin/cos in [-1, 1], correct day-of-week mapping
- `tests/test_features_microstructure.py` — HL spread > 0, close position in [0, 1]

**Acceptance criteria**:
- Calendar features use cyclical encoding (not raw integers)
- Volume ratio handles zero-volume days gracefully
- VWAP distance computed only when VWAP is available in data

### Phase 2.4: Cross-Asset Signals

**Files to create**:
- `app/ml/features/cross_asset.py`

**Modify**:
- `app/ml/providers/polygon_provider.py` — Add `fetch_reference_ohlcv(ticker, from_date, to_date)` method
- `app/ml/providers/mock_provider.py` — Add mock SPY data generation

**Tests**:
- `tests/test_features_cross_asset.py` — Correlation values between -1 and 1, relative strength sign correctness

**Acceptance criteria**:
- Cross-asset features are optional (`cross_asset: bool = False` by default)
- Graceful handling when SPY data fetch fails (log warning, skip cross-asset features)
- Date alignment between ticker and SPY data handled correctly (join on date)

### Phase 2.5: Pipeline Integration

**Modify**:
- `app/ml/preprocessing/pipeline.py` — Wire `FeatureEngine` into the pipeline
- `app/ml/models/schemas.py` — Add `FeatureGroupConfig` to `TrainingConfig`, update valid features set
- `app/ml/models/api_schemas.py` — Add `feature_groups` to API schemas

**Tests**:
- `tests/test_pipeline_features.py` — End-to-end: mock OHLCV → feature engine → pipeline → scaled sequences
- Verify no look-ahead bias (features at time t use only data from t-1 and earlier)
- Verify scaler is still fit on training data only

**Acceptance criteria**:
- Training with `feature_groups={"technical": True, "lag_features": True}` produces valid results
- Backward compatible: training with no `feature_groups` uses original OHLCV features only
- Feature names are stored in model metadata for reproducibility

### Phase 2.6: Feature Selection & Importance Analysis

**Files to create**:
- `app/ml/features/selection.py` — Feature selection utilities

**Implementation**:
- Mutual information scoring: `sklearn.feature_selection.mutual_info_regression`
- Correlation matrix computation with threshold-based pruning (drop features with |corr| > 0.95)
- Feature importance ranking from XGBoost (preparation for Level 2B)

**Tests**:
- `tests/test_features_selection.py` — Verify MI scores are non-negative, correlation pruning removes correct features

**Acceptance criteria**:
- Given 61 features, identify top-N by mutual information with target
- Flag highly correlated feature pairs for manual review
- Return feature ranking as structured data (for frontend visualization later)

---

## Detailed Function Signatures

### `technical.py`

```python
import pandas as pd
import pandas_ta as ta

def compute_technical_indicators(
    df: pd.DataFrame,
    rsi_length: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_length: int = 20,
    bb_std: float = 2.0,
    atr_length: int = 14,
    adx_length: int = 14,
    stoch_k: int = 14,
    stoch_d: int = 3,
    cci_length: int = 20,
    willr_length: int = 14,
    cmf_length: int = 20,
    mfi_length: int = 14,
) -> pd.DataFrame:
    """
    Compute technical indicators using pandas-ta.

    Input DataFrame must have columns: open, high, low, close, volume
    Returns DataFrame with original columns + indicator columns added.
    Does NOT drop NaN rows (caller handles warm-up trimming).
    """
    ...
```

### `volatility.py`

```python
import numpy as np
import pandas as pd

def parkinson_volatility(high: pd.Series, low: pd.Series, window: int = 20) -> pd.Series:
    """
    Parkinson (1980) range-based volatility estimator.
    σ² = (1 / 4n·ln2) · Σ[ln(H_i/L_i)]²
    ~5x more efficient than close-to-close estimator.
    """
    ...

def garman_klass_volatility(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20
) -> pd.Series:
    """
    Garman-Klass (1980) volatility estimator.
    σ² = (1/n) · Σ[0.5·(ln(H/L))² - (2ln2 - 1)·(ln(C/O))²]
    ~7.4x more efficient than close-to-close estimator.
    """
    ...

def rogers_satchell_volatility(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20
) -> pd.Series:
    """
    Rogers-Satchell (1991) volatility estimator.
    Handles non-zero drift (trending markets).
    σ² = (1/n) · Σ[ln(H/C)·ln(H/O) + ln(L/C)·ln(L/O)]
    """
    ...

def compute_volatility_features(
    df: pd.DataFrame,
    windows: list[int] | None = None,
    vol_window: int = 20,
) -> pd.DataFrame:
    """
    Compute all volatility features.
    Returns DataFrame with original columns + volatility columns added.
    """
    ...
```

### `lag_features.py`

```python
import pandas as pd

def compute_lag_features(
    df: pd.DataFrame,
    lag_periods: list[int] | None = None,
    rolling_windows: list[int] | None = None,
    momentum_periods: list[int] | None = None,
    sma_window: int = 20,
) -> pd.DataFrame:
    """
    Compute lagged returns, rolling statistics, and momentum features.

    Requires 'log_return' column in df (computed upstream in pipeline).
    Requires 'close' column for momentum and mean reversion.

    Default lag_periods: [1, 2, 3, 5, 10, 21]
    Default rolling_windows: [5, 10, 20]
    Default momentum_periods: [5, 10, 21]
    """
    ...
```

### `calendar_features.py`

```python
import numpy as np
import pandas as pd

def cyclical_encode(values: pd.Series, period: int) -> tuple[pd.Series, pd.Series]:
    """
    Encode cyclical feature as sin/cos pair.
    sin(2π · value / period), cos(2π · value / period)
    """
    ...

def compute_calendar_features(
    df: pd.DataFrame,
    market_holidays: list[str] | None = None,
) -> pd.DataFrame:
    """
    Compute calendar features from timestamp column.

    market_holidays: list of date strings from Polygon market status API.
    If None, days_since_holiday feature is skipped.
    """
    ...
```

### `cross_asset.py`

```python
import pandas as pd
from ..providers.protocols import MarketDataProvider

async def fetch_reference_data(
    provider: MarketDataProvider,
    reference_ticker: str,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Fetch OHLCV for a reference ticker (e.g., SPY)."""
    ...

def compute_cross_asset_features(
    df: pd.DataFrame,
    reference_df: pd.DataFrame,
    correlation_window: int = 20,
) -> pd.DataFrame:
    """
    Compute cross-asset features by joining reference ticker data.

    Joins on date. If dates don't align perfectly (holidays),
    forward-fills reference data.
    """
    ...
```

### `engine.py`

```python
import pandas as pd
from .config import FeatureGroupConfig
from ..providers.protocols import MarketDataProvider

class FeatureEngine:
    def __init__(
        self,
        config: FeatureGroupConfig | None = None,
        provider: MarketDataProvider | None = None,
    ):
        self.config = config or FeatureGroupConfig()
        self.provider = provider

    def compute(
        self,
        df: pd.DataFrame,
        ticker: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame:
        """
        Orchestrate all feature computations.

        1. Compute log_return if not present
        2. Run each enabled feature group
        3. Drop NaN warm-up rows
        4. Return enriched DataFrame

        Args:
            df: Raw OHLCV DataFrame
            ticker: Needed for cross-asset features (to know what we're analyzing)
            from_date/to_date: Needed for cross-asset reference data fetch
        """
        ...

    def get_feature_names(self) -> list[str]:
        """Return list of all feature names that will be generated given current config."""
        ...

    @property
    def warmup_bars(self) -> int:
        """Minimum bars needed before first valid feature row."""
        ...
```

---

## API Schema Changes

### TrainRequest (additions)

```python
class TrainRequest(BaseModel):
    # ... existing fields ...

    # New: Feature engineering configuration
    feature_groups: FeatureGroupsRequest | None = None

class FeatureGroupsRequest(BaseModel):
    """Which feature groups to enable for training."""
    technical: bool = True
    volatility: bool = True
    lag_features: bool = True
    volume: bool = True
    calendar: bool = True
    cross_asset: bool = False       # Opt-in (extra API call)
    microstructure: bool = True

    # Optional customization (advanced users)
    lag_periods: list[int] | None = None        # Default: [1,2,3,5,10,21]
    rolling_windows: list[int] | None = None    # Default: [5,10,20]
```

### TrainJobResult (additions)

```python
class TrainJobResult(BaseModel):
    # ... existing fields ...

    # New: Feature information
    features_used: list[str] | None = None          # All feature names used in training
    feature_count: int | None = None                 # Total number of features
    feature_groups_enabled: dict[str, bool] | None = None  # Which groups were active
```

---

## Warm-Up Period Handling

Features with lookback windows create NaN values at the start of the data. Strategy:

1. **Fetch extra data**: Request `warm_up_bars` extra days before `from_date` from Polygon
   - `warm_up_bars = max(macd_slow, max(rolling_windows), vol_window) + 10` (buffer)
   - Default: `max(26, 20, 20) + 10 = 36` bars
   - Round up to 50 bars for safety

2. **Compute features on full data** (including warm-up period)

3. **Trim warm-up rows**: After feature computation, drop all rows before the originally requested `from_date`

4. **Verify no NaN**: Assert no NaN values remain after trimming. If any exist, the warm-up was insufficient → raise error with diagnostic info.

This approach is cleaner than imputing NaN values, which would introduce artificial data.

---

## Look-Ahead Bias Prevention

The existing pipeline already shifts features by 1 timestep (`df[features].shift(1)`). With engineered features:

1. **Technical indicators** (RSI, MACD, etc.): Computed using data up to and including time `t`. After shift, the model at time `t` sees indicators from time `t-1`. This is correct — you know yesterday's RSI before today's open.

2. **Lag features**: `lag_ret_1` is already the return at `t-1`. After shift, it becomes `t-2`. This is overly conservative. **Fix**: Lag features should NOT be shifted again — they are already lagged by definition. The engine should mark lag features as "pre-shifted" so the pipeline skips the shift for them.

3. **Calendar features**: Day-of-week at time `t` is known before market open. No shift needed. Mark as "pre-shifted".

4. **Cross-asset features**: SPY return at `t-1` is known before today's open. Already lagged. Mark as "pre-shifted".

**Implementation**: `FeatureEngine.compute()` returns a tuple: `(DataFrame, list[str])` where the second element is the list of feature names that should NOT be shifted (because they are inherently lagged or point-in-time).

---

## Testing Strategy

### Unit Tests (per feature module)

| Test File | What It Validates |
|-----------|-------------------|
| `test_features_technical.py` | Each indicator output shape, value range, NaN handling, parameter customization |
| `test_features_volatility.py` | GK/Parkinson/RS formulas against hand-calculated values, edge cases (constant prices) |
| `test_features_lag.py` | Lag alignment (no off-by-one), rolling window correctness, momentum sign |
| `test_features_volume.py` | Volume ratio > 0, OBV direction matches price direction |
| `test_features_calendar.py` | Cyclical encoding range [-1, 1], correct day mapping, holiday distance |
| `test_features_cross_asset.py` | Date alignment, correlation bounds [-1, 1], missing data handling |
| `test_features_microstructure.py` | HL spread > 0, close position in [0, 1] |
| `test_features_engine.py` | Full pipeline integration, warm-up trimming, feature count, config toggles |

### Integration Test

| Test | What It Validates |
|------|-------------------|
| `test_pipeline_with_features.py` | Mock OHLCV → FeatureEngine → pipeline → scaled sequences. No NaN, no look-ahead, correct shapes |

### Look-Ahead Bias Test

A dedicated test that verifies no future data leaks into features:
```python
def test_no_lookahead_bias():
    """
    Strategy: Modify last row's close price.
    If any feature at time t-1 changes, there's a leak.
    """
    df_original = make_test_data(100)
    df_modified = df_original.copy()
    df_modified.iloc[-1, df_modified.columns.get_loc('close')] *= 1.5  # spike last close

    features_orig = engine.compute(df_original)
    features_mod = engine.compute(df_modified)

    # All rows except the last should be identical
    assert features_orig.iloc[:-1].equals(features_mod.iloc[:-1])
```

---

## Dependencies

### Already Installed (no changes to requirements.txt)
- `pandas-ta` — Technical indicators
- `pandas` — DataFrames, rolling, shift
- `numpy` — Volatility formulas
- `scikit-learn` — `mutual_info_regression` for feature selection

### No New Dependencies Required

All 61 features can be computed with the existing stack. The 4 volatility estimators (GK, Parkinson, RS, close-to-close) are simple numpy formulas — no need for `mlfinlab` or `volatility-trading` packages.

---

## File Count Summary

| Action | Files | Description |
|--------|-------|-------------|
| **Create** | 10 | 8 feature module files + 1 `__init__.py` + 0 new deps |
| **Modify** | 5 | pipeline.py, schemas.py, api_schemas.py, polygon_provider.py, mock_provider.py |
| **Tests** | 9 | 8 unit test files + 1 integration test |
| **Total** | 24 | |

---

## Execution Order

```
Phase 2.1  ─── technical.py + volatility.py + engine.py (partial) + config.py ──── ~Day 1-2
Phase 2.2  ─── lag_features.py                                                 ──── ~Day 2
Phase 2.3  ─── volume_features.py + calendar_features.py + microstructure.py   ──── ~Day 3
Phase 2.4  ─── cross_asset.py + provider changes                               ──── ~Day 3-4
Phase 2.5  ─── Pipeline integration + schema changes                           ──── ~Day 4-5
Phase 2.6  ─── Feature selection utilities                                      ──── ~Day 5
            ─── All tests written alongside each phase                          ────
```

After Level 2 is complete, the pipeline will accept a `feature_groups` config and automatically compute 61 engineered features from the same Polygon OHLCV data. This directly feeds into Level 2B (XGBoost), which thrives on rich tabular features.
