# Phase 1 & 2 Deep Dive: Insights, Alpha Models, and How They Compare to the Research Lab

**Date:** 2026-04-10

---

## Your Research Lab vs LEAN's Insight/Alpha System: A Side-by-Side

You already have a sophisticated signal evaluation pipeline in the Research Lab. The question is: where does LEAN's Insight system overlap, where does it differ, and what new things does it unlock?

Here's the honest answer: **your Research Lab and LEAN's Insight system solve the same fundamental problem — "is my prediction signal actually good?" — but they operate at different stages of the workflow and measure different things.**

### Research Lab Feature Runner: What It Does

Your Feature Runner (`research/runner.py`) takes a **raw feature** (like `momentum_5m` or `rsi_14`) and asks: "does this feature have predictive power over future returns?"

It measures this through:
- **Information Coefficient (IC):** Spearman rank correlation between the feature value and the 15-minute forward return, computed daily
- **Stationarity testing:** ADF + KPSS to verify the feature doesn't drift
- **Quantile analysis:** Bin the feature into quantiles, check if higher quantile = higher return (monotonicity)
- **Robustness:** Structural break detection, monthly IC breakdown, regime-specific IC

The validation gate is: `|mean IC| >= 0.03 AND p < 0.05 AND stationary AND monotonic`

### Research Lab Signal Engine: What It Does

Your Signal Engine (`research/signal/engine.py`) takes a **validated feature** and asks: "can this be turned into a tradable signal, and how good is that signal?"

It measures this through:
- **Z-score standardization** with train/test split
- **Threshold optimization** across a grid of thresholds and cost assumptions
- **Walk-forward validation** (rolling train/test windows, OOS Sharpe tracking)
- **Graduation criteria:** Net Sharpe > 0.75, Max DD < 15%, 60%+ OOS windows positive, regime coverage, parameter stability
- **Alpha decay detection** via OOS Sharpe trend slope
- **Signal behavior metrics:** Hit rate, avg win/loss, skewness of active returns

### LEAN's Insight System: What It Does

LEAN's Insight system takes a **real-time trading decision** and asks: "during the backtest, was this specific prediction correct at this specific moment?"

An Insight is emitted by an Alpha Model at bar time T and says: *"I predict SPY will go UP by 0.5% over the next 75 minutes with 80% confidence."* Then, 75 minutes later, the InsightManager checks what actually happened and scores it:

- **Direction Score (0-1):** Did the price go in the predicted direction? Binary: 1 if correct, 0 if wrong.
- **Magnitude Score (0-1):** How close was the predicted magnitude to the actual magnitude? Closer = higher score.
- **Reference Value tracking:** The price at emission (`reference_value`) vs the price at expiry (`reference_value_final`)

---

## The Key Difference: Static Research vs Live Backtest Evaluation

| Dimension | Your Research Lab | LEAN Insights (Phase 1 & 2) |
|---|---|---|
| **When evaluated** | After the fact, on historical data as a batch | During the backtest, bar by bar, in real time |
| **What's being tested** | A feature's statistical relationship to forward returns | A specific prediction at a specific moment |
| **Granularity** | Aggregate statistics (mean IC, overall Sharpe) | Per-prediction scoring (each Insight gets its own score) |
| **Context** | Feature values correlated with returns | Full trading context: entry signal + indicator state + price at that moment |
| **Scoring** | IC, t-stat, quantile spread | Direction accuracy, magnitude accuracy, per-insight |
| **Multi-model** | One feature at a time | Multiple Alpha Models emit insights simultaneously; scored and compared |
| **Connection to execution** | Separate from backtest trades | Insights flow into Portfolio Construction → orders |

**Think of it this way:**
- Research Lab answers: "Is RSI_14 a predictive feature for AAPL?"  
- Insights answer: "At 10:15 AM on March 5th, my EMA crossover model said SPY would go up — was it right?"

---

## What New Insights (Literally) You Gain from Phase 1 & 2

### 1. Per-Prediction Accuracy Tracking

Right now, your backtest output is aggregate: 63 trades, 69.84% win rate, Sharpe 1.2. That tells you the strategy works overall, but not *when* it works and *when* it doesn't.

With Insights, every single prediction gets scored individually. You can now ask:
- "My strategy made 63 predictions — which 19 were wrong and what was different about those moments?"
- "When RSI was 52 vs 68, were predictions equally accurate?"
- "Did predictions made on Monday mornings score differently than Friday afternoons?"

**Concrete example from your SpyEmaCrossoverAlgorithm:**

Today, a trade entry looks like this in your trade log:
```
ENTRY: 2024-06-03 10:30 Price=526.92 EMA5=526.8127 EMA10=526.5432 RSI=58.71
EXIT:  2024-06-03 11:45 Price=527.59 PnL=0.67 (0.13%) WIN
```

With Phase 1 & 2, you'd also have an Insight record:
```
Insight #37:
  Symbol: SPY
  Direction: UP
  Magnitude: 0.051% (EMA gap / price)
  Confidence: 0.72 (derived from RSI position in 50-70 band)
  Period: 75 minutes
  Reference Price: $526.92
  Final Price: $527.59
  Direction Score: 1.0 (correct — price went up)
  Magnitude Score: 0.38 (predicted 0.051%, actual was 0.13% — underestimated)
  Source Model: "EmaCross_EMA5_EMA10_RSI14"
```

That magnitude score of 0.38 is new information — it tells you the model consistently **underestimates** the move when it's right, which has implications for position sizing.

### 2. Confidence Calibration

This is something your Research Lab can't do today because it operates on features, not discrete predictions.

Once your Alpha Model emits confidence values, you can build a **calibration curve**: "When my model says 80% confidence, does it actually win 80% of the time?"

```
Confidence Band | Predictions | Actual Win Rate | Calibrated?
0.50–0.60       |     12      |      58%        | ✅ Good
0.60–0.70       |     28      |      64%        | ⚠️ Slightly overconfident  
0.70–0.80       |     18      |      83%        | ✅ Underconfident (good!)
0.80–1.00       |      5      |      60%        | ❌ Way overconfident
```

This tells you: high-confidence predictions aren't actually your best predictions. The model is most accurate in the 0.70-0.80 band. You could use this to adjust position sizing — bet smaller on "high confidence" signals.

### 3. Multi-Alpha Model Comparison

Phase 2 introduces the `AlphaModel` interface, which means you can run multiple signal generators simultaneously against the same data and compare them head-to-head.

Right now your strategies are monolithic — the SpyEmaCrossoverAlgorithm contains the signal logic AND the execution logic in one class. With Alpha Models, you separate them:

```
EmaCrossAlpha  → emits Insights → scored
RsiReversalAlpha → emits Insights → scored
MomentumRsiAlpha → emits Insights → scored
```

Then you can build a comparison table:

```
Alpha Model            | Insights | Direction Acc | Avg Magnitude Score | Overlap w/ Others
EmaCross_5_10_RSI14    |    63    |    69.8%      |       0.42          |    38% w/ Momentum
RsiReversal_14_30_70   |    91    |    58.2%      |       0.55          |    12% w/ EmaCross
MomentumRsiStochastic  |    47    |    74.5%      |       0.31          |    38% w/ EmaCross
```

This is the beginning of **ensemble strategy development** — if two Alpha Models agree on a prediction, confidence should be higher. If they disagree, maybe reduce sizing. Your Research Lab's batch runner runs features one at a time; the Insight system evaluates them in concert.

### 4. Temporal Analysis of Predictions

Because each Insight has `generated_time` and `close_time`, you can analyze prediction quality over time:

- **Alpha decay in real time:** Are later predictions worse than earlier ones? (Your walk-forward does this at the feature level, but Insights do it per-prediction)
- **Time-of-day effects:** Are 10:00 AM predictions better than 3:30 PM predictions?
- **Regime-conditional accuracy:** During high-vol weeks, do predictions degrade? (Your Research Lab has regime coverage, but not per-prediction regime tagging)

### 5. Prediction-to-Execution Gap Analysis

This is entirely new and impossible without Insights. Today, your system can tell you:
- Feature X has good IC (Research Lab) ✅
- Strategy using Feature X made money (Backtest) ✅

But it can't tell you: **"The signal was right 70% of the time, but the strategy only captured 50% of the predicted magnitude because of fill slippage and timing."**

With Insights, you can compute:
```
Prediction accuracy:  70% direction correct
Execution capture:    Average predicted magnitude: 0.08%
                     Average actual magnitude captured: 0.05%
                     Capture ratio: 62.5%
```

That 37.5% gap between what the signal predicted and what the portfolio captured is actionable intelligence — it tells you whether to work on better signals or better execution.

---

## How Phase 1 & 2 Build ON TOP of the Research Lab (Not Replace It)

The Research Lab and the Insight system are **complementary layers**, not alternatives:

```
LAYER 1 — Feature Discovery (Research Lab Feature Runner)
  "Is momentum_5m predictive of 15-min returns for AAPL?"
  Output: IC, t-stat, quantile spread, stationarity
  
LAYER 2 — Signal Validation (Research Lab Signal Engine)  
  "Can momentum_5m be turned into a tradable threshold signal?"
  Output: Walk-forward OOS Sharpe, graduation grade, alpha decay
  
LAYER 3 — Prediction Tracking (Phase 1: Insights)        ← NEW
  "This specific EMA crossover signal at 10:15 AM predicted UP 0.05%"
  Output: Per-prediction direction/magnitude scores
  
LAYER 4 — Alpha Model Evaluation (Phase 2: Alpha Models)  ← NEW
  "How does the EMA crossover Alpha compare to the RSI Reversal Alpha?"
  Output: Model-level accuracy, confidence calibration, ensemble overlap
```

Features that pass the Research Lab gauntlet (Layer 1 & 2) become Alpha Models (Layer 3 & 4). The Research Lab is your R&D pipeline; the Insight system is your production evaluation framework.

---

## Phase 1 Implementation Detail: The Insight Data Model

### What We Build

```
PythonDataService/app/engine/framework/
├── __init__.py
├── insight.py           # Insight, InsightType, InsightDirection, InsightScore
├── insight_manager.py   # Tracks insights, scores them when they expire
└── insight_scorer.py    # Default scoring function (direction + magnitude)
```

### Insight Class (Ported from LEAN's Insight.cs)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import uuid4


class InsightType(Enum):
    """What the insight is predicting."""
    PRICE = 0        # Predicting price movement
    VOLATILITY = 1   # Predicting volatility change


class InsightDirection(Enum):
    """Predicted direction of movement."""
    DOWN = -1
    FLAT = 0
    UP = 1


@dataclass
class InsightScore:
    """Scoring container — matches LEAN's InsightScore.cs.
    
    Scores are clamped to [0, 1]. Once finalized, they can't be updated.
    """
    direction: float = 0.0       # 0 = wrong direction, 1 = correct
    magnitude: float = 0.0       # 0 = way off, 1 = exact magnitude match
    is_final_score: bool = False
    updated_time_utc: datetime | None = None

    def set_score(self, score_type: str, value: float) -> None:
        if self.is_final_score:
            return
        clamped = max(0.0, min(1.0, value))
        if score_type == "direction":
            self.direction = clamped
        elif score_type == "magnitude":
            self.magnitude = clamped
        self.updated_time_utc = datetime.utcnow()

    def finalize(self, time: datetime) -> None:
        self.is_final_score = True
        self.updated_time_utc = time


@dataclass
class Insight:
    """A structured prediction — the core of LEAN's Alpha framework.
    
    Ported from Lean/Common/Algorithm/Framework/Alphas/Insight.cs.
    Every field maps to the LEAN class; we drop the .NET-specific
    serialization machinery and keep the data + behavior.
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid4()))
    group_id: str | None = None
    source_model: str = ""
    tag: str = ""

    # What we're predicting
    symbol: str = ""
    type: InsightType = InsightType.PRICE
    direction: InsightDirection = InsightDirection.FLAT

    # Prediction parameters
    period: timedelta = field(default_factory=lambda: timedelta(minutes=75))
    magnitude: float | None = None      # Expected % change (e.g., 0.005 = 0.5%)
    confidence: float | None = None     # 0.0 to 1.0
    weight: float | None = None         # Portfolio weight intent

    # Timing
    generated_time: datetime = field(default_factory=datetime.utcnow)
    close_time: datetime = field(default_factory=datetime.utcnow)

    # Reference values for scoring
    reference_value: Decimal = Decimal(0)        # Price when insight was created
    reference_value_final: Decimal = Decimal(0)  # Price when insight expired

    # Score
    score: InsightScore = field(default_factory=InsightScore)

    def __post_init__(self) -> None:
        if self.close_time == self.generated_time:
            self.close_time = self.generated_time + self.period

    def is_active(self, utc_time: datetime) -> bool:
        return utc_time < self.close_time

    def is_expired(self, utc_time: datetime) -> bool:
        return utc_time >= self.close_time

    @staticmethod
    def price(
        symbol: str,
        direction: InsightDirection,
        period: timedelta,
        magnitude: float | None = None,
        confidence: float | None = None,
        weight: float | None = None,
        source_model: str = "",
        tag: str = "",
    ) -> Insight:
        """Factory method matching LEAN's Insight.Price(...)."""
        return Insight(
            symbol=symbol,
            type=InsightType.PRICE,
            direction=direction,
            period=period,
            magnitude=magnitude,
            confidence=confidence,
            weight=weight,
            source_model=source_model,
            tag=tag,
        )
```

### InsightManager (Ported from LEAN's InsightManager.cs)

```python
@dataclass
class InsightManager:
    """Tracks all insights and scores them when they expire.
    
    Ported from Lean/Common/Algorithm/Framework/Alphas/Analysis/InsightManager.cs.
    Simplified: we don't need the C# Dictionary<Symbol, List<Insight>> because
    Python's defaultdict does the same job.
    """
    _insights: dict[str, list[Insight]] = field(default_factory=lambda: defaultdict(list))
    _all_insights: list[Insight] = field(default_factory=list)
    _scorer: InsightScoreFunction | None = None

    def add(self, insight: Insight, current_price: Decimal) -> None:
        """Register a new insight and set its reference price."""
        insight.reference_value = current_price
        self._insights[insight.symbol].append(insight)
        self._all_insights.append(insight)

    def step(self, utc_time: datetime, current_prices: dict[str, Decimal]) -> list[Insight]:
        """Process time step — score any insights that have expired.
        
        Returns the list of newly-scored (finalized) insights.
        """
        newly_scored = []
        for symbol, insights in self._insights.items():
            price = current_prices.get(symbol, Decimal(0))
            for insight in insights:
                if insight.is_expired(utc_time) and not insight.score.is_final_score:
                    insight.reference_value_final = price
                    if self._scorer:
                        self._scorer.score(insight)
                    insight.score.finalize(utc_time)
                    newly_scored.append(insight)
        return newly_scored

    def get_active_insights(self, utc_time: datetime) -> list[Insight]:
        return [i for ilist in self._insights.values() 
                for i in ilist if i.is_active(utc_time)]

    def get_all_insights(self) -> list[Insight]:
        return list(self._all_insights)

    def get_insights_for_symbol(self, symbol: str) -> list[Insight]:
        return list(self._insights.get(symbol, []))
```

### Default Insight Scorer

```python
class DefaultInsightScoreFunction:
    """Scores insights based on direction accuracy and magnitude proximity.
    
    Direction: 1.0 if price moved in predicted direction, 0.0 otherwise.
    Magnitude: 1.0 - |predicted_magnitude - actual_magnitude| / max(|predicted|, |actual|)
    """
    
    def score(self, insight: Insight) -> None:
        if insight.reference_value == 0:
            return

        actual_return = float(
            (insight.reference_value_final - insight.reference_value) 
            / insight.reference_value
        )

        # Direction scoring
        if insight.direction == InsightDirection.UP:
            direction_correct = actual_return > 0
        elif insight.direction == InsightDirection.DOWN:
            direction_correct = actual_return < 0
        else:  # FLAT
            direction_correct = abs(actual_return) < 0.001  # within 0.1%
        
        insight.score.set_score("direction", 1.0 if direction_correct else 0.0)

        # Magnitude scoring (only if magnitude was predicted)
        if insight.magnitude is not None and insight.magnitude != 0:
            actual_mag = abs(actual_return)
            predicted_mag = abs(insight.magnitude)
            max_mag = max(actual_mag, predicted_mag)
            if max_mag > 0:
                mag_score = 1.0 - abs(predicted_mag - actual_mag) / max_mag
                insight.score.set_score("magnitude", mag_score)
```

### Changes to Existing Code (Minimal)

**Strategy base (base.py) — add emit_insight:**
```python
# In StrategyContext:
insight_manager: InsightManager = field(default_factory=InsightManager)

def emit_insight(self, insight: Insight) -> None:
    """Register a structured prediction. Optional — strategies that don't
    emit insights continue to work exactly as before."""
    price = self.portfolio.reference_price.get(insight.symbol, Decimal(0))
    insight.generated_time = self.current_time or insight.generated_time
    self.insight_manager.add(insight, price)
```

**BacktestEngine (engine.py) — add insight scoring step:**
```python
# After processing fills, before appending to equity_curve:
current_prices = {sym: portfolio.reference_price.get(sym, Decimal(0)) 
                  for sym in ctx.symbols}
ctx.insight_manager.step(minute_bar.end_time, current_prices)
```

**BacktestResult — add insights:**
```python
@dataclass
class BacktestResult:
    # ... existing fields ...
    insights: list[Insight] = field(default_factory=list)  # NEW
```

**That's it.** Existing strategies don't call `emit_insight()` and aren't affected at all.

---

## Phase 2 Implementation Detail: Alpha Models

### What We Build

```
PythonDataService/app/engine/framework/
├── alpha/
│   ├── __init__.py
│   ├── alpha_model.py              # Base interface
│   ├── ema_cross_alpha.py          # SpyEmaCrossover as an Alpha Model
│   ├── rsi_reversal_alpha.py       # RSI Mean Reversion as an Alpha Model
│   └── composite_alpha.py          # Run multiple alphas, merge insights
```

### AlphaModel Interface

```python
from abc import ABC, abstractmethod

class AlphaModel(ABC):
    """Base class for Alpha Models.
    
    Ported from LEAN's IAlphaModel. The key insight (pun intended) is
    separation of concerns: the Alpha Model ONLY generates predictions.
    It doesn't decide position sizing, risk limits, or execution.
    
    The Update method is called on each consolidated bar and returns
    a list of Insight objects. Returning an empty list means "no opinion."
    """
    
    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def update(self, context: StrategyContext, bar: TradeBar) -> list[Insight]:
        """Generate zero or more insights from new data."""
        ...

    def on_securities_changed(self, added: list[str], removed: list[str]) -> None:
        """Called when the universe changes. Override if your model needs
        to initialize per-symbol state (indicators, etc.)."""
        pass
```

### SpyEmaCrossover as an Alpha Model

This shows how the existing strategy logic maps into the Alpha Model pattern:

```python
class EmaCrossAlphaModel(AlphaModel):
    """The same EMA5/EMA10/RSI14 crossover logic, but it emits Insights
    instead of calling set_holdings().
    
    This is the Alpha Model equivalent of SpyEmaCrossoverAlgorithm.
    The signal detection logic is identical; only the output changes
    from "buy SPY" to "I predict SPY will go UP."
    """
    
    def __init__(self, fast_period: int = 5, slow_period: int = 10, 
                 rsi_period: int = 14, hold_bars: int = 5,
                 consolidation_minutes: int = 15) -> None:
        self._fast_period = fast_period
        self._slow_period = slow_period
        self._rsi_period = rsi_period
        self._hold_bars = hold_bars
        self._consolidation_minutes = consolidation_minutes
        
        # Per-symbol indicator state
        self._indicators: dict[str, _IndicatorState] = {}
    
    def update(self, context: StrategyContext, bar: TradeBar) -> list[Insight]:
        state = self._get_or_create(bar.symbol)
        
        # Update indicators (same logic as SpyEmaCrossoverAlgorithm)
        state.ema_fast.update(bar.end_time, bar.close)
        state.ema_slow.update(bar.end_time, bar.close)
        state.rsi.update(bar.end_time, bar.close)
        
        if not (state.ema_fast.is_ready and state.ema_slow.is_ready and state.rsi.is_ready):
            # Warmup: still update crossover state (LEAN trap #5)
            state.update_crossover()
            return []
        
        ema_fast_val = state.ema_fast.current_value
        ema_slow_val = state.ema_slow.current_value
        rsi_val = state.rsi.current_value
        
        current_above = ema_fast_val > ema_slow_val
        ema_gap = ema_fast_val - ema_slow_val
        
        insights = []
        
        # Same entry conditions as SpyEmaCrossoverAlgorithm
        fresh_crossover = current_above and not state.prev_above
        gap_ok = ema_gap >= Decimal("0.20")
        rsi_ok = Decimal(50) <= rsi_val <= Decimal(70)
        
        if fresh_crossover and gap_ok and rsi_ok:
            # Instead of set_holdings() → emit an Insight
            period = timedelta(minutes=self._consolidation_minutes * self._hold_bars)
            
            # Compute confidence from RSI position within the valid band
            rsi_position = float((rsi_val - 50) / 20)  # 0.0 at RSI=50, 1.0 at RSI=70
            confidence = 0.5 + 0.3 * (1.0 - abs(rsi_position - 0.5))  # Peak at RSI=60
            
            insights.append(Insight.price(
                symbol=bar.symbol,
                direction=InsightDirection.UP,
                period=period,
                magnitude=float(ema_gap / bar.close),  # Normalized gap as expected move
                confidence=confidence,
                source_model=f"EmaCross_{self._fast_period}_{self._slow_period}_RSI{self._rsi_period}",
                tag=f"EMA{self._fast_period}={ema_fast_val:.4f} "
                    f"EMA{self._slow_period}={ema_slow_val:.4f} "
                    f"RSI={rsi_val:.2f}",
            ))
        
        state.prev_above = current_above
        return insights
```

### CompositeAlphaModel (Run Multiple Alphas Together)

```python
class CompositeAlphaModel(AlphaModel):
    """Runs multiple alpha models and merges their insights.
    
    Ported from LEAN's CompositeAlphaModel. This is how you build
    ensemble strategies — each sub-model emits its own insights,
    and they're all tracked and scored independently.
    """
    
    def __init__(self, *models: AlphaModel) -> None:
        self._models = list(models)
    
    def update(self, context: StrategyContext, bar: TradeBar) -> list[Insight]:
        all_insights = []
        for model in self._models:
            insights = model.update(context, bar)
            all_insights.extend(insights)
        return all_insights
```

---

## What You Can Analyze After Phase 1 & 2

Once implemented, here are the concrete new analyses available:

### Analysis 1: Prediction Accuracy Report
```
SpyEmaCrossover Alpha — 63 Insights Emitted
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Direction Accuracy:     69.8% (44/63 correct)
Avg Magnitude Score:    0.42
Avg Confidence Emitted: 0.67
Confidence Calibration: Underconfident (actual accuracy > stated confidence)

By Quarter:
  2024-Q2: 72.2% direction accuracy (13/18)
  2024-Q3: 66.7% direction accuracy (10/15)
  2024-Q4: 75.0% direction accuracy (9/12)
  2025-Q1: 63.6% direction accuracy (7/11)
  2025-Q2: 71.4% direction accuracy (5/7)
```

### Analysis 2: Signal-to-Execution Gap
```
Prediction vs Capture:
  Avg predicted magnitude: 0.042%
  Avg actual magnitude when correct: 0.087%
  Avg actual magnitude when wrong: -0.058%
  
  → The model consistently underestimates winning moves
  → Losses are smaller than wins even when direction is wrong
  → Suggests increasing position size is safe given the asymmetry
```

### Analysis 3: Multi-Alpha Comparison (after porting 2+ strategies)
```
                        EmaCross  RsiReversal  MomentumRsi
Insights emitted:          63        91           47
Direction accuracy:       69.8%     58.2%        74.5%
Avg magnitude score:       0.42      0.55         0.31
Avg confidence:            0.67      0.61         0.73
Overlapping signals:              38% overlap      12% overlap
When both agree:                    82% accuracy
When they disagree:                 51% accuracy
```

### Analysis 4: Temporal Patterns
```
By Time of Day:
  9:30-10:30: 78% accuracy (morning momentum captured well)
  10:30-12:00: 65% accuracy (midday noise degrades signal)
  12:00-14:00: 60% accuracy (lunch doldrums)
  14:00-16:00: 73% accuracy (afternoon trend resumes)
  
→ Consider suppressing signals in the 10:30-12:00 window
```

---

## Connection: Research Lab → Insight System Pipeline

After Phase 1 & 2, your full workflow becomes:

```
1. DISCOVER (Research Lab Feature Runner)
   Run momentum_5m on AAPL → IC = 0.05, stationary, monotonic → PASS
   
2. VALIDATE (Research Lab Signal Engine)
   Convert to threshold signal → Walk-forward OOS Sharpe = 1.1 → Grade: A
   
3. IMPLEMENT (Alpha Model — Phase 2)
   Code MomentumAlphaModel that emits Insights based on momentum_5m
   
4. EVALUATE (Insight Scoring — Phase 1)
   Run backtest → 47 insights → 74.5% direction accuracy
   → Confidence calibration: well-calibrated
   → Magnitude: underestimates by 30%
   
5. COMPARE (Composite Alpha — Phase 2)
   Run alongside EmaCrossAlpha → 38% overlap → ensemble accuracy 82%
   
6. SIZE & RISK (Phase 3 & 4 — future)
   Use confidence scores to weight positions
   Use magnitude scores to set stop distances
```

The Research Lab feeds INTO the Insight system. Features validated in the lab become Alpha Models. The Insight system then evaluates those Alpha Models in a realistic backtest context — with fills, slippage, and timing — that the Research Lab's statistical analysis can't capture.

---

## Recommended Next Step

**Start with Phase 1 — just the Insight data model.** Write the `Insight`, `InsightScore`, and `InsightManager` classes as described above. Then modify `SpyEmaCrossoverAlgorithm` to emit insights alongside its existing `set_holdings()` calls (dual-mode: it still trades normally AND records predictions). Run the bit-exact parity test to confirm nothing breaks. Then inspect the scored insights — that's your first new analytical output.

Phase 2 (Alpha Model refactoring) can follow immediately after, since it's just restructuring the same signal logic behind a clean interface.
