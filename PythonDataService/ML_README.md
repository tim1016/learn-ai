# LSTM Stock Price Predictor

A deep learning module using Long Short-Term Memory (LSTM) networks to forecast stock prices based on historical time-series data.

Built on top of the existing Polygon.io data pipeline in `PythonDataService`. Modular, testable, baseline-verified, and API-ready.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12 |
| Deep Learning | TensorFlow / Keras |
| Data Normalization | scikit-learn (`MinMaxScaler`) |
| Market Data | Polygon.io (via existing `PolygonClientService`) |
| Data Manipulation | pandas, NumPy |
| Visualization | Matplotlib |
| CLI | Click |
| Testing | pytest |

---

## Project Structure

```
app/ml/
├── __init__.py
├── __main__.py                  # Enables: python -m app.ml train ...
├── cli.py                       # Click CLI with train/validate commands
├── protocols.py                 # MarketDataProvider Protocol (swappable data source)
├── providers/
│   ├── polygon_provider.py      # Wraps existing PolygonClientService
│   └── mock_provider.py         # Deterministic sine-wave data for testing
├── models/
│   └── schemas.py               # TrainingConfig, TrainingResult, WalkForwardResult
├── preprocessing/
│   ├── pipeline.py              # Full pipeline: fetch → scale → window → split
│   ├── scaler.py                # MinMaxScaler wrapper with JSON save/load
│   └── windowing.py             # Sliding window sequence creation
├── training/
│   ├── lstm_model.py            # LSTM architecture builder
│   ├── trainer.py               # Training loop with callbacks + baseline comparison
│   └── baseline.py              # Naive persistence model
├── evaluation/
│   ├── metrics.py               # RMSE, MAE, MAPE, directional accuracy
│   ├── visualization.py         # Matplotlib plots (predictions, residuals, history)
│   └── walk_forward.py          # Walk-forward temporal cross-validation
└── services/
    └── prediction_service.py    # High-level orchestration for CLI and future API

notebooks/
└── lstm_experiments.ipynb       # Interactive Jupyter notebook

tests/ml/
├── conftest.py                  # Shared fixtures
├── test_protocols.py            # Provider protocol compliance
├── test_preprocessing.py        # Scaler, windowing, pipeline tests
├── test_lstm_model.py           # Model architecture tests
├── test_baseline.py             # Baseline model tests
├── test_trainer.py              # Training smoke tests (slow)
├── test_metrics.py              # Metric calculation tests
├── test_walk_forward.py         # Walk-forward validation tests (slow)
└── test_prediction_service.py   # End-to-end service tests (slow)

trained_models/                  # Gitignored — saved .keras + .scaler.json files
```

---

## Quick Start

### 1. Install Dependencies

```bash
cd PythonDataService
pip install -r requirements.txt
```

### 2. Train with Mock Data (no API key needed)

```bash
python -m app.ml.cli train --ticker AAPL --from-date 2022-01-01 --to-date 2024-01-01 --mock
```

### 3. Train with Real Polygon Data

Requires `POLYGON_API_KEY` in your `.env` file:

```bash
python -m app.ml.cli train --ticker AAPL --from-date 2024-01-01 --to-date 2026-01-01 --epochs 100
```

### 4. Walk-Forward Validation

```bash
python -m app.ml.cli validate --ticker AAPL --from-date 2022-01-01 --to-date 2024-01-01 --mock --folds 3
```

### 5. Run Tests

```bash
# Fast unit tests only
python -m pytest tests/ml/ -x -k "not slow"

# All tests including slow integration tests
python -m pytest tests/ml/ -x --timeout=120
```

---

## Data Pipeline

```
Polygon.io → DataFrame → Feature Selection → MinMaxScaler [0,1] → Sliding Windows → Temporal Split
```

| Step | Description |
|------|------------|
| **Fetch** | OHLCV daily bars via Polygon.io (2-year max on Starter plan) |
| **Preprocess** | Extract selected features, normalize to [0, 1] with MinMaxScaler |
| **Window** | Rolling 60-day sequences → shape `(samples, 60, n_features)` |
| **Split** | Temporal train/test split (80/20, no shuffle — preserves time order) |

---

## Model Architecture

```
Input (60, 1)
  → LSTM (50 units, return_sequences=True)
  → Dropout (0.2)
  → LSTM (50 units)
  → Dropout (0.2)
  → Dense (1)

Optimizer:  Adam (lr=0.001)
Loss:       Mean Squared Error (MSE)
Callbacks:  EarlyStopping (patience=10), ReduceLROnPlateau (patience=5)
Parameters: ~20-30k (varies with input features)
```

---

## CLI Reference

### Commands

| Command | Description |
|---------|-------------|
| `train` | Train an LSTM model on historical data |
| `validate` | Run walk-forward cross-validation |

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--ticker` | *required* | Stock ticker symbol (e.g., AAPL, MSFT) |
| `--from-date` | *required* | Start date in YYYY-MM-DD format |
| `--to-date` | *required* | End date in YYYY-MM-DD format |
| `--epochs` | 50 | Number of training epochs |
| `--sequence-length` | 60 | Lookback window in trading days |
| `--features` | close | Comma-separated: close, volume, high, low, open, returns |
| `--mock` | false | Use deterministic test data instead of Polygon |
| `--output-dir` | trained_models | Directory for saved models and plots |
| `--folds` | 5 | Number of walk-forward validation folds (validate only) |

### Example Output

```
============================================================
  Training Complete: AAPL
============================================================
  Val RMSE:          0.042315
  Baseline RMSE:     0.038721
  Improvement:       -9.28%
  Best Epoch:        12/50
  Model Saved:       trained_models/AAPL_lstm.keras
  Plots Saved:       trained_models/plots
============================================================

  WARNING: LSTM did NOT beat the naive persistence baseline.
  The model may be ineffective for this data/configuration.
```

---

## Validation & Verification

### Baseline Comparison (Critical)

Every training run automatically compares the LSTM against a **naive persistence model**:

```
prediction(t+1) = price(t)
```

If the LSTM doesn't beat baseline RMSE, it reports **negative improvement** and warns that the model is ineffective. This prevents false confidence in the predictions.

### Walk-Forward Validation

Instead of a single train/test split, the data is divided into sequential temporal folds:

1. Train on earlier period → test on next period
2. Roll the window forward
3. Repeat for each fold

Key properties:
- **Scaler is fit per-fold** to prevent look-ahead bias
- Reports: avg RMSE, MAE, MAPE, Directional Accuracy
- More honest than a single split

### Data Validation

The pipeline validates at each stage:
- No NaN values in training data
- No duplicate timestamps
- Proper numeric types (`float64`)
- Scaling range verified: `min >= 0`, `max <= 1`
- Inverse transform roundtrip accuracy checked in tests

---

## Swappable Data Providers

The module uses a `MarketDataProvider` Protocol pattern:

| Provider | Use Case |
|----------|----------|
| `PolygonDataProvider` | Production — wraps existing Polygon.io client |
| `MockDataProvider` | Testing — deterministic sine wave, no API key needed |

**Add your own** by implementing a class with a `fetch_ohlcv()` method:

```python
class MyProvider:
    def fetch_ohlcv(
        self, ticker: str, from_date: str, to_date: str,
        timespan: str = "day", multiplier: int = 1,
    ) -> list[dict]:
        # Return list of dicts with: timestamp, open, high, low, close, volume
        ...
```

---

## Jupyter Notebook

```bash
cd PythonDataService
jupyter notebook notebooks/lstm_experiments.ipynb
```

The notebook provides step-by-step experimentation:
1. Data exploration and raw price visualization
2. Preprocessing: scaling and windowing with visual checks
3. Single training run with loss curves
4. Actual vs predicted plots and residual analysis
5. Baseline comparison
6. Walk-forward validation
7. Hyperparameter experiments (sequence length, LSTM units)

---

## Known Limitations

1. **Small dataset** — Polygon Starter plan provides 2 years of daily data (~504 bars). After windowing with `sequence_length=60`, only ~355 training samples remain. This is marginal for deep learning.

2. **Price prediction difficulty** — LSTMs on raw close prices often fail to beat naive persistence. Stock prices behave close to a random walk. The baseline comparison honestly reports when this happens.

3. **Educational focus** — This is a learning and experimentation tool, **not financial advice**. Do not use predictions for actual trading decisions.

4. **Reproducibility** — TensorFlow results may vary slightly across CPU/GPU and hardware. Set `tf.random.set_seed()` for best-effort reproducibility.

---

## Future Extensions

| Extension | Description |
|-----------|-------------|
| **Multi-feature model** | Use close + volume + high-low spread as input features |
| **Predict returns** | More stationary than raw prices → better learning signal |
| **Technical indicators** | Add RSI, MACD, SMA as input features (already available in the service) |
| **FastAPI endpoints** | Add `POST /api/predictions/train` to expose via the Angular dashboard |
| **Minute-level data** | Use intraday data for a larger training set |
| **Attention mechanism** | Replace or augment LSTM with transformer-based architecture |

---

## Testing

```bash
# Run only fast unit tests
python -m pytest tests/ml/ -x -k "not slow"

# Run all tests (includes training, ~60-120s)
python -m pytest tests/ml/ -x --timeout=120

# Run with verbose output
python -m pytest tests/ml/ -v

# Run a specific test file
python -m pytest tests/ml/test_preprocessing.py -v
```

### Test Coverage

| Test File | Tests | Speed |
|-----------|-------|-------|
| `test_protocols.py` | Provider compliance, determinism | Fast |
| `test_preprocessing.py` | Scaler, windowing, pipeline | Fast |
| `test_lstm_model.py` | Architecture, compilation, I/O shapes | Medium |
| `test_baseline.py` | Persistence model correctness | Fast |
| `test_metrics.py` | RMSE, MAE, MAPE, directional accuracy | Fast |
| `test_trainer.py` | Training smoke test | Slow (~30s) |
| `test_walk_forward.py` | Walk-forward validation | Slow (~60s) |
| `test_prediction_service.py` | End-to-end orchestration | Slow (~30s) |
