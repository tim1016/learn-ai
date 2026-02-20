Production-Grade Fix (Recommended)

Use walk-forward / expanding window scaling:

For each time step t:

Fit scaler using data up to t-1

Transform only the current prediction window

This prevents future information from leaking backward.

2. Non-Stationarity of Raw Prices
Problem

Raw prices are trending and non-stationary.

LSTM may learn:

Trend persistence

Regime drift

Market growth bias

Instead of learning predictive structure.

Statistical Verification

Use:

Augmented Dickey-Fuller (ADF)

KPSS test

Phillips-Perron test

Example:

from statsmodels.tsa.stattools import adfuller

result = adfuller(series)
print("ADF p-value:", result[1])

If p-value > 0.05 → likely non-stationary.

Recommended Fix

Use log returns instead of raw prices:

𝑟
𝑡
=
log
⁡
(
𝑃
𝑡
/
𝑃
𝑡
−
1
)
r
t
	​

=log(P
t
	​

/P
t−1
	​

)

Implementation:

import numpy as np

df["log_return"] = np.log(df["close"] / df["close"].shift(1))
df = df.dropna()

Benefits:

Removes long-term trend

Stabilizes variance

More statistically appropriate

Reduces overfitting

3. MinMax Compression of Outliers
Problem

MinMax scaling compresses extreme events:

Earnings gaps

Flash crashes

Volatility spikes

This distorts distribution shape and reduces tail sensitivity.

Better Alternatives
StandardScaler (Z-score)
from sklearn.preprocessing import StandardScaler
scaler = StandardScaler()

Best for approximately normal return distributions.

RobustScaler
from sklearn.preprocessing import RobustScaler
scaler = RobustScaler()

Uses median and IQR.
More stable under heavy tails.

Winsorization (Optional)

Clip extreme values:

lower = df["log_return"].quantile(0.01)
upper = df["log_return"].quantile(0.99)
df["log_return"] = df["log_return"].clip(lower, upper)

Prevents rare events from dominating gradients.

4. No Automated Stationarity Testing
Problem

No statistical verification that inputs are suitable for modeling.

This can allow:

Regime drift

Structural breaks

Non-stationary series

Invalid model assumptions

Add Statistical Guardrail

Before training:

Run ADF

Run KPSS

Log p-values

Warn or fail if non-stationary

Example rule:

if adf_p > 0.05 and kpss_p < 0.05:
    raise ValueError("Series appears non-stationary.")

This enforces discipline before training begins.

5. Look-Ahead Bias in VWAP
Problem

Daily VWAP is computed using full intraday data.

If predicting:

Close(t)

And using:

VWAP(t)

You are leaking same-day information into the model.

Proper Temporal Alignment

If predicting close(t), allowed features:

Close(t-1)

VWAP(t-1)

Volume(t-1)

All features must be shifted:

df[features] = df[features].shift(1)
df = df.dropna()

This ensures no look-ahead contamination.

6. Train/Test Splitting
Problem

Random splitting invalidates time series modeling.

Time must flow forward.

Correct Methods

Walk-forward validation

Expanding window validation

Rolling window backtesting

Example structure:

Train:   2015-2018
Test:    2019

Train:   2015-2019
Test:    2020

Never randomly shuffle time series.

7. Evaluation Metrics
Problem

Using only MSE or accuracy does not measure trading value.

A model can have low error but no trading edge.

Add Trading-Relevant Metrics

Directional accuracy

Sharpe ratio

Maximum drawdown

Profit factor

Hit rate

Turnover

Transaction-cost-adjusted returns

Without these, results are not economically meaningful.

8. Regime Sensitivity Testing

Test performance separately in:

Bull markets

Bear markets

High-volatility periods

Low-volatility periods

If performance collapses in one regime:

Model is fragile

Strategy is not robust

Priority Fix Order

Remove scaler leakage

Fix VWAP look-ahead

Replace random split with walk-forward validation

Convert raw prices to log returns

Replace MinMaxScaler

Add stationarity testing

Upgrade evaluation metrics

Final Goal

Transition from:

"Model predicts prices well"

To:

"Model produces statistically valid, economically meaningful, and leakage-free results under realistic trading conditions."

Recommended Next Phase

Implement leakage-safe pipeline

Add walk-forward backtesting engine

Add transaction cost modeling

Add slippage assumptions

Log all statistical diagnostics automatically