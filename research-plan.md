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

## Level 2B — Model Diversification: XGBoost

### Motivation

The LSTM is a sequence model — it ingests windows of time steps and learns temporal dependencies end-to-end. This works for capturing sequential patterns, but has significant limitations for a personal trading tool:

- **No feature interpretability** — you cannot determine which inputs drive predictions
- **Slow training** — GPU-heavy, epochs take minutes to hours for modest gains
- **Suboptimal for tabular signals** — if RSI crossing 70 predicts a reversal, a decision tree finds that boundary in seconds; an LSTM needs thousands of examples to implicitly learn it

Gradient Boosted Trees (XGBoost) flip this paradigm. You engineer features explicitly (RSI, MACD, volume ratios, rolling stats), feed them as flat feature vectors, and XGBoost provides **feature importance scores** — telling you exactly which indicators are driving alpha. For a tool informing real trading decisions, this interpretability is essential: you need to understand *why* a model says buy or sell.

**Key insight from ML research**: For tabular data (which is what financial indicators become after feature engineering), gradient boosted trees consistently outperform deep learning in benchmarks. Deep learning's advantage is in sequential/spatial pattern recognition — which matters for raw time series, but less so once you've extracted tabular features.

### Architectural Approach: Head-to-Head Comparison

XGBoost will be a **parallel model type** alongside LSTM, selectable by the user at training time. Both models share the same infrastructure (data fetching, evaluation metrics, walk-forward validation, job management, API layer) but differ in data shaping and model building.

```
                    ┌─────────────────────────────┐
                    │      TrainRequest            │
                    │  (model_type: "xgboost")     │
                    └──────────┬──────────────────┘
                               │
                    ┌──────────▼──────────────────┐
                    │    PredictionService         │
                    │    (orchestrator)             │
                    └──────────┬──────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌─────────────┐  ┌──────────────┐  ┌──────────────┐
    │ LSTMTrainer │  │ XGBTrainer   │  │ TFTTrainer   │
    │             │  │              │  │  (future)    │
    └──────┬──────┘  └──────┬───────┘  └──────┬───────┘
           │                │                  │
    ┌──────▼──────┐  ┌──────▼───────┐  ┌──────▼───────┐
    │  Windowing  │  │  Tabular     │  │  TFT-style   │
    │  (3D seqs)  │  │  (lag cols)  │  │  (static +   │
    │             │  │              │  │   temporal)   │
    └─────────────┘  └──────────────┘  └──────────────┘
```

**Shared infrastructure** (no duplication):
- Data fetching (Polygon provider)
- Feature engineering (Level 2 indicators)
- Scaling (fit on train only)
- Evaluation metrics (RMSE, MAE, Sharpe, drawdown, profit factor)
- Walk-forward validation
- Job management (async background training)
- API layer and frontend

**Model-specific** (new per model type):
- Data shaping (windowing vs tabular flattening)
- Model building and training
- Hyperparameter configuration
- Model persistence format
- Feature importance extraction (XGBoost-specific)

### LSTM vs XGBoost — Key Differences

| Aspect | LSTM (current) | XGBoost (proposed) |
|--------|---------------|-------------------|
| **Input shape** | 3D: `(samples, timesteps, features)` | 2D: `(samples, lag_features)` |
| **Feature engineering** | Minimal — learns from raw sequences | Rich — benefits from explicit indicators |
| **Training speed** | Minutes to hours (GPU) | Seconds to minutes (CPU) |
| **Interpretability** | Black box | Feature importance, SHAP values |
| **Hyperparameters** | LR, layers, dropout, units | `max_depth`, `n_estimators`, `learning_rate`, `subsample` |
| **Overfitting signal** | Validation loss divergence | Early stopping on eval metric |
| **Model persistence** | `.keras` file | `.json` or `.ubj` file |
| **Best at** | Sequential temporal patterns | Non-linear feature interactions, decision boundaries |

### XGBoost Feature Set (Expanded)

XGBoost thrives on rich tabular features. Beyond the raw OHLCV used by LSTM, XGBoost should receive:

| Category | Features | Why XGBoost Benefits |
|----------|----------|---------------------|
| **Momentum** | RSI(14), MACD(12,26,9), Stochastic %K/%D | Clear decision boundaries (RSI > 70 = overbought) |
| **Volatility** | Bollinger Band width, ATR(14), historical vol | Tree splits on volatility thresholds naturally |
| **Volume** | OBV, volume ratio (current / 20-day avg), VWAP distance | Discrete volume signals are tabular by nature |
| **Trend** | SMA/EMA crossovers (50/200), ADX | Binary crossover signals are perfect for trees |
| **Rolling stats** | 5/10/20-day rolling mean, std, skew of returns | Statistical summaries compress sequence info into tabular form |
| **Calendar** | Day of week, month, days to options expiry | Categorical features trees handle natively |
| **Lag features** | Close returns at t-1, t-2, ..., t-N | Autoregressive signal without needing sequence model |

### Data Shaping: Windowing vs Tabular Flattening

LSTM receives a 3D tensor — a sliding window of N timesteps across M features. XGBoost receives a 2D matrix where each row contains all information for a single prediction:

```
LSTM input (1 sample):
  ┌──────────────────────────────────┐
  │ t-60: [close, vol, rsi, macd]   │
  │ t-59: [close, vol, rsi, macd]   │
  │ ...                              │
  │ t-1:  [close, vol, rsi, macd]   │
  └──────────────────────────────────┘
  Shape: (60, 4)

XGBoost input (1 sample):
  ┌────────────────────────────────────────────────────────────────┐
  │ rsi_t-1, macd_t-1, vol_ratio_t-1, atr_t-1, return_t-1,      │
  │ return_t-2, return_t-5, return_t-21, bollinger_width_t-1,    │
  │ adx_t-1, day_of_week, ...                                    │
  └────────────────────────────────────────────────────────────────┘
  Shape: (1, N_features)
```

Key difference: XGBoost doesn't need raw sequential history — it gets **pre-computed summaries** (indicators, lags, rolling stats) that compress temporal information into a single feature vector.

### XGBoost-Specific Outputs

Beyond standard metrics (RMSE, MAE, Sharpe, etc.), XGBoost training should return:

- **Feature importance (gain)** — which features contributed most to prediction accuracy
- **Feature importance (cover)** — which features were used to split the most samples
- **SHAP values** (optional, computationally heavier) — per-prediction feature contributions
- **Tree depth / complexity stats** — for diagnosing overfitting

These should be returned in the `TrainJobResult` and visualized in the frontend (bar chart of feature importances).

### Frontend Integration

- **Model type selector**: Dropdown in the train form — "LSTM", "XGBoost" (future: "TFT", "Ensemble")
- **Feature importance chart**: Bar chart showing top N features by importance (XGBoost only)
- **Side-by-side comparison view**: Train both models on same data, compare metrics in a table
- **Model list**: Existing model list extended with `model_type` column

### Walk-Forward Validation

The existing walk-forward validation structure is already model-agnostic in concept (expanding window, per-fold scaling, metric aggregation). Changes needed:
- Abstract the trainer instantiation to accept model type
- XGBoost folds train much faster, so more folds become practical (10-20 vs 3-5 for LSTM)
- Same metrics apply: RMSE, MAE, directional accuracy, Sharpe, drawdown, profit factor

### Future Evolution: Temporal Fusion Transformer (TFT)

The architecture is designed with a third model slot in mind. TFT would eventually replace vanilla LSTM as the "sequence expert":

- **Why TFT over LSTM**: Designed for multi-horizon forecasting, handles static covariates (sector, market cap) alongside temporal features, provides attention-based interpretability
- **Framework**: PyTorch + `pytorch-forecasting` (different from Keras LSTM)
- **When**: After XGBoost is validated and the model abstraction layer is proven
- **Prerequisite**: XGBoost integration proves the multi-model architecture works

**Recommendation**: XGBoost first (fast to implement, immediate interpretability value), TFT later as a v3 model type. XGBoost will also validate whether the feature engineering pipeline and model abstraction layer work before investing in TFT.

### Implementation Phases for XGBoost

```
Phase 2B-1: Research & Design
  - Finalize feature list and indicator calculations
  - Define ModelTrainer abstraction (protocol/interface)
  - Plan API schema changes (model_type field, feature importance response)
  - Identify dependencies (xgboost, shap packages)

Phase 2B-2: Feature Engineering Module
  - Build indicator calculations (pandas-ta or manual)
  - Testable, reusable functions: compute_rsi(), compute_macd(), etc.
  - Tabular flattening: convert time series + indicators into lag-feature rows
  - Unit tests for feature correctness

Phase 2B-3: XGBTrainer
  - XGBoost model building with configurable hyperparameters
  - Early stopping on validation metric
  - Feature importance extraction (gain, cover)
  - Model persistence (.json format)
  - Baseline comparison (same persistence baseline)
  - Unit tests for trainer

Phase 2B-4: Pipeline Integration
  - Abstract PredictionService to dispatch by model_type
  - Tabular data shaping step in DataPipeline
  - Walk-forward validation support for XGBoost
  - Integration tests

Phase 2B-5: API & Frontend
  - model_type field in TrainRequest/ValidateRequest
  - Feature importance in TrainJobResult
  - Frontend: model type dropdown, feature importance chart
  - Side-by-side comparison view

Phase 2B-6: Validation & Benchmarking
  - Run both models on same tickers/date ranges
  - Compare metrics across multiple walk-forward runs
  - Document findings: which model wins when and why
```

### Research Questions

1. Does XGBoost outperform LSTM on your specific tickers and timeframes?
2. Which features have the most predictive power? (Feature importance analysis)
3. How many lag features are optimal? (Too few = underfitting, too many = noise)
4. Does XGBoost degrade less in volatile regimes vs LSTM?
5. What's the training speed difference in practice for your data sizes?
6. Can feature importance from XGBoost inform which features to feed into LSTM/TFT?

### Key Libraries

| Library | Purpose | Notes |
|---------|---------|-------|
| `xgboost` | Gradient boosted trees | Core model library |
| `pandas-ta` | Technical indicator calculations | Already in project dependencies |
| `shap` | SHAP value computation | Optional, adds interpretability |

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
Phase 1 (Foundations)            -> Level 1 + Level 2          ✅ LEVEL 1 DONE
  Fix scaler leakage, test stationarity, add technical indicators
  Add returns as default target, test ADF before training

Phase 2 (Model Diversification) -> Level 2B                   ⬅ NEXT
  Build feature engineering module (technical indicators)
  Implement XGBTrainer alongside LSTMTrainer
  Abstract PredictionService for multi-model dispatch
  Add model_type to API, feature importance to responses
  Frontend: model selector, feature importance chart, comparison view
  Benchmark XGBoost vs LSTM head-to-head

Phase 3 (Better Predictions)    -> Level 3 + Level 5
  Switch to return prediction or direction classification
  Add MC Dropout for uncertainty quantification
  Add prediction intervals to the frontend charts

Phase 4 (Architecture)          -> Level 4
  Replace vanilla LSTM with Temporal Fusion Transformer (TFT)
  Benchmark TFT vs XGBoost vs LSTM
  Explore ensemble of top-performing models

Phase 5 (Strategy)              -> Level 6
  Build backtesting module with vectorbt
  Signal generation with confidence filter
  Position sizing with Kelly criterion
  Compute Sharpe/Sortino/drawdown

Phase 6 (Operations)            -> Level 7
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
