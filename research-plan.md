# LSTM Predictive Strategy — Research Plan

## Current State

A **functional prototype**: train LSTM on OHLCV, walk-forward validation, persistence baseline comparison, async job management, basic frontend. It answers: *"Can an LSTM fit historical price data?"*

## Goal

A system that answers: *"What will likely happen next, how confident am I, and what should I do about it?"*

---

## Level 1 — Fix Foundational Flaws (Statistical Rigor) ✅ COMPLETED

All foundational issues have been fixed. The pipeline now produces statistically valid, leakage-free results.

| Gap | Fix Applied |
|-----|------------|
| **Scaler leakage** | Scaler is now fit on training data only. Data is split before scaling — `fit_transform(train)`, `transform(test)`. |
| **Non-stationarity** | Log returns support added (`log_returns=True`). Transforms raw prices to stationary percentage changes. |
| **Single normalization** | Three scaler types available: `StandardScaler` (z-score, default), `RobustScaler` (median/IQR), `MinMaxScaler`. |
| **No stationarity testing** | ADF + KPSS tests run automatically on training target. Results returned in training output with warnings for non-stationary data. |
| **Look-ahead in VWAP** | All features shifted by 1 timestep (`df[features].shift(1)`) to prevent same-day look-ahead bias. |
| **Outlier handling** | Optional winsorization clips extreme values at configurable quantile bounds (default 1st/99th percentile). |
| **Evaluation metrics** | Trading metrics added: Sharpe ratio, max drawdown, profit factor — computed per fold in walk-forward validation. |

### Files Changed
- `PythonDataService/app/ml/preprocessing/pipeline.py` — Leakage fix, feature shifting, log returns, winsorization
- `PythonDataService/app/ml/preprocessing/scaler.py` — Multi-scaler support (Standard/Robust/MinMax)
- `PythonDataService/app/ml/preprocessing/stationarity.py` — ADF/KPSS stationarity testing (new)
- `PythonDataService/app/ml/evaluation/metrics.py` — Sharpe, max drawdown, profit factor
- `PythonDataService/app/ml/evaluation/walk_forward.py` — Trading metrics per fold
- `PythonDataService/app/ml/models/schemas.py` — New config fields
- `PythonDataService/app/ml/models/api_schemas.py` — New API fields

---

## Level 2 — Feature Engineering (Better Inputs)

The model currently sees raw OHLCV. Markets are driven by much richer signals.

| Feature Category | Examples | Research Keywords |
|-----------------|----------|-------------------|
| **Technical indicators** | RSI, MACD, Bollinger Bands, ATR, OBV | *ta-lib, pandas-ta* (already installed) |
| **Volatility features** | Historical volatility, Garman-Klass, Parkinson | *realized volatility estimators* |
| **Calendar features** | Day of week, month, days to expiry, quarter-end | *seasonality in equities, calendar anomalies* |
| **Lagged features** | Returns at t-1, t-5, t-21 (day, week, month) | *autoregressive features, partial autocorrelation* |
| **Cross-asset signals** | VIX, SPY correlation, sector ETF, 10Y yield | *cross-asset momentum, risk-on/risk-off regimes* |
| **Volume profile** | Relative volume, volume momentum, OBV divergence | *volume-price analysis* |
| **Microstructure** | Bid-ask spread proxy (high-low), transaction count | *market microstructure noise* |

### Research Questions

1. Which features have the most predictive power for your target? (Use mutual information or Granger causality)
2. How do you handle features with different frequencies (daily price vs weekly macro)?
3. Feature selection: how do you prevent overfitting with many features? (LASSO, recursive feature elimination)

---

## Level 3 — Prediction Target Redesign

Currently: predict **next day's scaled close price**. This is arguably the hardest and least useful target.

| Alternative Target | Why | Research Keywords |
|-------------------|-----|-------------------|
| **N-day return direction** | Binary (up/down) — simpler, more actionable | *classification LSTM, binary cross-entropy* |
| **Return magnitude buckets** | Multi-class: big up, small up, flat, small down, big down | *ordinal classification, quantile regression* |
| **Multi-step ahead** | Predict next 5/10/21 days, not just tomorrow | *seq2seq LSTM, encoder-decoder, multi-horizon forecasting* |
| **Volatility forecasting** | Predict next-day realized volatility (for options/sizing) | *GARCH, HAR-RV, volatility LSTM* |
| **Regime classification** | Trending vs mean-reverting vs choppy | *hidden Markov models, regime-switching, market state detection* |
| **Probability distribution** | Output a distribution, not a point estimate | *mixture density networks, quantile regression neural networks* |

### Research Questions

1. Is direction prediction (binary classification) more profitable than price regression?
2. Can you combine regression + classification (predict return AND confidence)?
3. What prediction horizon gives the best signal-to-noise ratio for your data?

---

## Level 4 — Architecture Improvements

| Architecture | Why | Research Keywords |
|-------------|-----|-------------------|
| **Attention mechanism** | Let the model learn *which* timesteps matter most | *temporal attention, self-attention for time series* |
| **Bidirectional LSTM** | For training-time pattern recognition (not real-time) | *BiLSTM, sequence labeling* |
| **CNN-LSTM hybrid** | CNN extracts local patterns, LSTM captures temporal dependencies | *1D convolution + LSTM, temporal convolutional networks (TCN)* |
| **Transformer-based** | State-of-the-art for sequence modeling | *Temporal Fusion Transformer (TFT), PatchTST, iTransformer* |
| **Ensemble methods** | Combine multiple models for robustness | *model averaging, stacking, bagging for time series* |
| **GRU** | Simpler than LSTM, often comparable performance, faster training | *GRU vs LSTM benchmark* |

### Research Questions

1. Does attention reveal interpretable patterns (e.g., model pays attention to earnings dates)?
2. Is a Transformer worth the complexity vs a well-tuned LSTM for daily stock data?
3. How does an ensemble of simple models compare to one complex model?

---

## Level 5 — Uncertainty Quantification

**Most impactful gap** for a real strategy. Without uncertainty, you can't size positions.

| Technique | What It Gives You | Research Keywords |
|----------|------------------|-------------------|
| **MC Dropout** | Run inference N times with dropout on -> prediction distribution | *Monte Carlo dropout, Bayesian approximation, Gal & Ghahramani 2016* |
| **Quantile regression** | Predict 10th/50th/90th percentile instead of mean | *quantile loss, pinball loss, prediction intervals* |
| **Conformal prediction** | Distribution-free prediction intervals with guaranteed coverage | *conformal prediction, adaptive conformal inference* |
| **Mixture Density Networks** | Output a Gaussian mixture — captures multi-modal outcomes | *MDN, Bishop 1994* |
| **Deep ensembles** | Train N models, use disagreement as uncertainty | *Lakshminarayanan et al. 2017* |

### Research Questions

1. Can MC Dropout give you calibrated prediction intervals for stock prices?
2. How do conformal prediction intervals perform in regime changes?
3. Can you use uncertainty to filter: *only trade when the model is confident*?

---

## Level 6 — From Prediction to Strategy

| Component | Purpose | Research Keywords |
|-----------|---------|-------------------|
| **Signal generation** | Convert prediction -> buy/sell/hold signal with threshold | *signal processing, prediction threshold optimization* |
| **Position sizing** | Use uncertainty to scale position size (Kelly criterion) | *Kelly criterion, fractional Kelly, risk parity* |
| **Risk management** | Dynamic stop-loss based on predicted volatility + ATR | *adaptive stop-loss, volatility-based exits* |
| **Backtesting engine** | Simulate strategy on historical data with realistic costs | *vectorbt, backtrader, zipline, transaction costs, slippage* |
| **Performance metrics** | Sharpe, Sortino, max drawdown, Calmar, win rate | *risk-adjusted returns, drawdown analysis* |
| **Regime filter** | Only trade in favorable regimes (e.g., trending + low vol) | *regime detection, market state classification* |
| **Portfolio level** | Multi-ticker strategy with correlation-aware allocation | *mean-variance optimization, risk budgeting* |

### Research Questions

1. What's the minimum directional accuracy needed to be profitable after costs?
2. How sensitive is P&L to prediction threshold (e.g., trade only when predicted return > 0.5%)?
3. Can you use the LSTM's confidence to dynamically adjust position size?

---

## Level 7 — Operational Maturity

| Component | Purpose | Research Keywords |
|-----------|---------|-------------------|
| **Experiment tracking** | MLflow/W&B to compare runs | *MLOps, experiment tracking* |
| **Hyperparameter optimization** | Optuna/Ray Tune for automated tuning | *Bayesian optimization, hyperband* |
| **Model registry** | Version models, promote to staging/production | *model versioning, ML model registry* |
| **Drift detection** | Detect when model performance degrades | *concept drift, data drift, PSI, KS test* |
| **Retraining pipeline** | Automatic retraining on schedule or drift trigger | *continuous training, MLOps pipeline* |
| **Inference API** | Real-time prediction endpoint with loaded model | *TF Serving, ONNX Runtime, FastAPI inference* |

---

## Implementation Phases

```
Phase 1 (Foundations)        -> Level 1 + Level 2
  Fix scaler leakage, test stationarity, add technical indicators
  Add returns as default target, test ADF before training

Phase 2 (Better Predictions) -> Level 3 + Level 5
  Switch to return prediction or direction classification
  Add MC Dropout for uncertainty quantification
  Add prediction intervals to the frontend charts

Phase 3 (Architecture)       -> Level 4
  Add attention mechanism to existing LSTM
  Benchmark against GRU and simple CNN-LSTM
  Try ensemble of 3-5 models

Phase 4 (Strategy)           -> Level 6
  Build backtesting module with vectorbt
  Signal generation with confidence filter
  Position sizing with Kelly criterion
  Compute Sharpe/Sortino/drawdown

Phase 5 (Operations)         -> Level 7
  MLflow integration for experiment tracking
  Optuna for hyperparameter search
  Inference endpoint for live predictions
```

---

## Key Papers & Resources

### Foundational
- **Gal & Ghahramani (2016)** — *Dropout as a Bayesian Approximation* — MC Dropout for uncertainty
- **Bishop (1994)** — *Mixture Density Networks* — outputting probability distributions
- **Lakshminarayanan et al. (2017)** — *Simple and Scalable Predictive Uncertainty Estimation using Deep Ensembles*

### Financial ML
- **Lopez de Prado (2018)** — *Advances in Financial Machine Learning* — walk-forward, feature importance, backtesting pitfalls
- **Dixon, Halperin, Bilokon (2020)** — *Machine Learning in Finance* — LSTM applications, risk management
- **Bao, Yue, Rao (2017)** — *A deep learning framework for financial time series using stacked autoencoders and LSTM*

### Architecture
- **Lim, Arik et al. (2021)** — *Temporal Fusion Transformers for Interpretable Multi-horizon Time Series Forecasting*
- **Vaswani et al. (2017)** — *Attention Is All You Need* — Transformer fundamentals
- **Nie et al. (2023)** — *PatchTST: A Time Series is Worth 64 Words* — patch-based Transformer for time series

### Strategy & Risk
- **Kelly (1956)** — *A New Interpretation of Information Rate* — Kelly criterion for position sizing
- **Vovk, Gammerman, Shafer (2005)** — *Algorithmic Learning in a Random World* — conformal prediction theory
