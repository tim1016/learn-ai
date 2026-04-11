# LEAN Algorithm Framework Integration Plan

**Date:** 2026-04-10  
**Goal:** Bring our backtest engine closer to LEAN's full Algorithm Framework — Insights, Alpha Models, Portfolio Construction, Risk Management, Execution Models, and Insight Scoring — while keeping our app lightweight and efficient.

---

## Current State — What We Already Have

Our engine already reproduces LEAN's backtest semantics bit-exactly for single-symbol strategies. Here's what's in place:

| LEAN Component | Our Equivalent | Status |
|---|---|---|
| QCAlgorithm base class | `Strategy` (base.py) | ✅ Solid |
| Consolidators | `TradeBarConsolidator` | ✅ Bit-exact |
| Indicators (EMA, RSI, SMA) | `engine/indicators/` | ✅ Bit-exact |
| Portfolio / Cash accounting | `Portfolio` (portfolio.py) | ✅ Working |
| FillModel (Market orders) | `FillModel` (fill_model.py) | ✅ Two modes |
| Trade logging | `LoggedTrade` dataclass | ✅ Working |
| LEAN Statistics suite (25+ fields) | `lean_statistics.py` | ✅ Validated |
| BacktestEngine main loop | `engine.py` | ✅ Single-symbol |
| Strategy implementations | SPY EMA Crossover + 4 others | ✅ Working |

## What's Missing — The LEAN Algorithm Framework

LEAN's full framework has a **five-stage pipeline** that our engine doesn't implement yet:

```
Universe Selection → Alpha Model → Portfolio Construction → Risk Management → Execution
                        ↓
                    Insight objects (structured predictions)
                        ↓
                    InsightManager (tracking + scoring)
                        ↓
                    PortfolioTarget objects (position sizing)
                        ↓
                    Risk-adjusted targets
                        ↓
                    Orders
```

Our current engine skips all of this — strategies call `set_holdings()` and `liquidate()` directly, which is equivalent to LEAN's "Classic Algorithm" mode (no framework pipeline). The framework pipeline is what enables Insight tracking, prediction scoring, modular strategy composition, and systematic risk management.

---

## Implementation Plan — 5 Phases

### Phase 1: Insight Data Model + EmitInsight API

**What:** Port the `Insight` class and `InsightScore` class. Add `emit_insight()` to the Strategy base.

**Why this first:** Insights are the foundation everything else plugs into. Without them, none of the downstream models (Portfolio Construction, Risk Management) can work. This phase has zero impact on existing strategies — it's purely additive.

**Files to create:**

```
PythonDataService/app/engine/framework/
├── __init__.py
├── insight.py          # Insight, InsightType, InsightDirection, InsightScore
├── insight_manager.py  # InsightManager (collection + scoring orchestration)
└── insight_scorer.py   # DefaultInsightScoreFunction (direction + magnitude scoring)
```

**Key classes to port from LEAN:**

```python
# insight.py — ported from Lean/Common/Algorithm/Framework/Alphas/Insight.cs

class InsightType(Enum):
    PRICE = 0
    VOLATILITY = 1

class InsightDirection(Enum):
    DOWN = -1
    FLAT = 0
    UP = 1

@dataclass
class InsightScore:
    direction: float = 0.0      # 0–1, was the direction prediction correct?
    magnitude: float = 0.0      # 0–1, how close was the magnitude prediction?
    is_final_score: bool = False
    updated_time_utc: datetime | None = None

@dataclass
class Insight:
    id: str                           # UUID
    group_id: str | None = None
    symbol: str
    type: InsightType = InsightType.PRICE
    direction: InsightDirection
    period: timedelta                  # how long the prediction should hold
    magnitude: float | None = None    # expected % change
    confidence: float | None = None   # 0–1 confidence
    weight: float | None = None       # portfolio weight intent
    source_model: str = ""            # which alpha model generated this
    tag: str = ""

    generated_time: datetime           # when emitted
    close_time: datetime               # generated_time + period

    reference_value: Decimal = Decimal(0)     # price at emission
    reference_value_final: Decimal = Decimal(0)  # price at close_time

    score: InsightScore = field(default_factory=InsightScore)

    def is_active(self, utc_time: datetime) -> bool: ...
    def is_expired(self, utc_time: datetime) -> bool: ...
```

**Changes to existing code:**

- `Strategy` base class (base.py): Add `emit_insight()` method that delegates to `StrategyContext`
- `StrategyContext`: Add `insight_manager: InsightManager` field, `emit_insight()` method
- `BacktestEngine`: After each time step, call `insight_manager.step(current_time)` to score expired insights

**Backward compatibility:** Existing strategies don't emit insights and continue to work unchanged. New strategies can optionally emit insights alongside their existing `set_holdings()` calls.

---

### Phase 2: Alpha Model Interface + Built-in Alpha Models

**What:** Create the `IAlphaModel` interface and port our existing strategies into Alpha Models that emit Insights.

**Why:** This separates "signal generation" from "position sizing" — the core architectural insight of LEAN's framework. Our current strategies mix both concerns in `on_bar()`. Splitting them lets us evaluate signal quality independently from execution quality.

**Files to create:**

```
PythonDataService/app/engine/framework/
├── alpha/
│   ├── __init__.py
│   ├── alpha_model.py              # IAlphaModel interface
│   ├── ema_cross_alpha.py          # Port of our SpyEmaCrossover as an Alpha Model
│   ├── rsi_alpha.py                # Port of RSI Mean Reversion as Alpha
│   ├── momentum_rsi_stochastic_alpha.py
│   └── constant_alpha.py           # Simple always-long/short for testing
```

**Key interface (from LEAN's `IAlphaModel`):**

```python
class AlphaModel(ABC):
    """Base class for alpha models.
    
    Ported from LEAN's IAlphaModel. The Update method receives new data
    and returns a list of Insight objects (predictions).
    """
    
    @abstractmethod
    def update(self, context: StrategyContext, bar: TradeBar) -> list[Insight]:
        """Generate insights from new data. Called on each consolidated bar."""
        ...
    
    def on_securities_changed(self, changes: list[str]) -> None:
        """Notification when the universe changes (symbols added/removed)."""
        ...
```

**Example — EMA Cross as an Alpha Model:**

```python
class EmaCrossAlphaModel(AlphaModel):
    def update(self, context, bar):
        # Same indicator logic as SpyEmaCrossoverAlgorithm
        # But instead of calling set_holdings(), emit an Insight:
        if fresh_crossover and gap_ok and rsi_ok:
            return [Insight(
                symbol=bar.symbol,
                direction=InsightDirection.UP,
                period=timedelta(minutes=75),  # 5 bars * 15 min
                magnitude=float(ema_gap / bar.close),
                confidence=self._compute_confidence(rsi_val, ema_gap),
                source_model="EmaCross_EMA5_EMA10_RSI14",
            )]
        return []
```

**Key benefit:** Once strategies are Alpha Models, we can:
1. Score their predictions independently (did the price actually go up?)
2. Combine multiple Alpha Models (ensemble)
3. Swap out Portfolio Construction without touching signal logic

---

### Phase 3: Portfolio Construction Model

**What:** Create the `IPortfolioConstructionModel` interface and implement Equal Weighting, Confidence Weighting, and Insight Weighting models.

**Why:** This is where Insights become actionable positions. Currently our strategies hardcode `set_holdings(SPY, 1.0)`. With Portfolio Construction, the sizing is a separate pluggable concern.

**Files to create:**

```
PythonDataService/app/engine/framework/
├── portfolio/
│   ├── __init__.py
│   ├── portfolio_construction_model.py    # Interface + PortfolioTarget
│   ├── equal_weighting.py                 # Equal weight across active insights
│   ├── confidence_weighting.py            # Weight by insight confidence
│   └── insight_weighting.py               # Weight by insight weight field
```

**Key classes:**

```python
@dataclass
class PortfolioTarget:
    """Ported from LEAN's PortfolioTarget.cs"""
    symbol: str
    quantity: Decimal          # target shares to hold
    tag: str = ""

    @staticmethod
    def percent(portfolio: Portfolio, symbol: str, percent: float) -> PortfolioTarget:
        """Create a target from a portfolio percentage."""
        ...

class PortfolioConstructionModel(ABC):
    @abstractmethod
    def create_targets(
        self, context: StrategyContext, insights: list[Insight]
    ) -> list[PortfolioTarget]:
        """Convert insights into portfolio targets."""
        ...
```

**How it flows:**

```
Alpha Model emits: [Insight(SPY, UP, confidence=0.8), Insight(QQQ, UP, confidence=0.6)]
    ↓
ConfidenceWeightingModel.create_targets():
    → [PortfolioTarget(SPY, 57 shares), PortfolioTarget(QQQ, 43 shares)]
```

---

### Phase 4: Risk Management Model

**What:** Create the `IRiskManagementModel` interface and implement Max Drawdown, Trailing Stop, and Max Exposure models.

**Why:** Risk management is currently implicit in our strategies (e.g., the 5-bar exit rule is a form of time-based risk management). Making it explicit and pluggable means we can apply consistent risk rules across all strategies without modifying them.

**Files to create:**

```
PythonDataService/app/engine/framework/
├── risk/
│   ├── __init__.py
│   ├── risk_management_model.py          # Interface
│   ├── max_drawdown_model.py             # Liquidate when drawdown exceeds threshold
│   ├── trailing_stop_model.py            # Trailing stop loss per position
│   └── max_unrealized_profit_model.py    # Take profit at threshold
```

**Key interface:**

```python
class RiskManagementModel(ABC):
    @abstractmethod
    def manage_risk(
        self, context: StrategyContext, targets: list[PortfolioTarget]
    ) -> list[PortfolioTarget]:
        """Adjust or override portfolio targets for risk compliance.
        
        Can reduce positions, liquidate entirely, or pass through unchanged.
        Multiple risk models are chained sequentially (LEAN's pattern).
        """
        ...
```

---

### Phase 5: Execution Model + Framework Engine

**What:** Create the `IExecutionModel` interface and build a `FrameworkEngine` that orchestrates the full pipeline (Alpha → Portfolio Construction → Risk → Execution), alongside the existing `BacktestEngine`.

**Why:** This completes the framework. The `FrameworkEngine` is the analog of LEAN's `QCAlgorithm.Framework.cs` — it wires all the models together and runs the insight scoring loop.

**Files to create:**

```
PythonDataService/app/engine/framework/
├── execution/
│   ├── __init__.py
│   ├── execution_model.py                # Interface
│   └── immediate_execution_model.py      # Submit market orders immediately
├── framework_engine.py                   # The full pipeline orchestrator
```

**FrameworkEngine — the full pipeline:**

```python
class FrameworkEngine:
    """Orchestrates the LEAN Algorithm Framework pipeline.
    
    This is the analog of LEAN's QCAlgorithm.OnFrameworkData():
    data → alpha.update() → insights → portfolio.create_targets() 
    → risk.manage_risk() → execution.execute()
    
    Runs alongside (not replacing) the existing BacktestEngine.
    """
    
    def __init__(
        self,
        alpha: AlphaModel | list[AlphaModel],
        portfolio_construction: PortfolioConstructionModel,
        risk_management: RiskManagementModel | list[RiskManagementModel],
        execution: ExecutionModel,
        insight_manager: InsightManager,
    ): ...
    
    def on_bar(self, context: StrategyContext, bar: TradeBar) -> None:
        # 1. Generate insights
        insights = []
        for alpha in self._alphas:
            insights.extend(alpha.update(context, bar))
        
        # 2. Register with InsightManager (sets reference prices, tracks)
        for insight in insights:
            self._insight_manager.add(insight)
        
        # 3. Portfolio construction: insights → targets
        targets = self._portfolio_construction.create_targets(context, insights)
        
        # 4. Risk management: adjust targets
        for risk_model in self._risk_models:
            targets = risk_model.manage_risk(context, targets)
        
        # 5. Execution: targets → orders
        self._execution.execute(context, targets)
        
        # 6. Score insights (check expired ones against actual prices)
        self._insight_manager.step(context.current_time)
```

---

## Frontend Integration — New UI Components

### Insight Dashboard (Angular)

Once the backend emits insights with scores, we need to display them:

```
Frontend/src/app/components/strategy-lab/
├── insight-panel/
│   ├── insight-panel.component.ts      # Main insight list + filters
│   ├── insight-panel.component.html
│   ├── insight-panel.component.scss
│   ├── insight-score-card.component.ts  # Individual insight with score viz
│   └── insight-timeline.component.ts    # Timeline of predictions vs outcomes
```

**Key visualizations:**
- Insight accuracy over time (direction score trend)
- Prediction vs actual price overlay chart
- Confidence calibration plot (predicted confidence vs actual hit rate)
- Alpha model comparison table (which model scores best?)

### GraphQL API Extensions

```graphql
type Insight {
  id: String!
  symbol: String!
  direction: InsightDirection!
  period: Float!
  magnitude: Float
  confidence: Float
  generatedTime: DateTime!
  closeTime: DateTime!
  referenceValue: Float!
  referenceValueFinal: Float
  score: InsightScore
  sourceModel: String!
}

type InsightScore {
  direction: Float!
  magnitude: Float!
  isFinalScore: Boolean!
}

type BacktestInsightResults {
  insights: [Insight!]!
  totalInsights: Int!
  averageDirectionScore: Float!
  averageMagnitudeScore: Float!
  predictionAccuracy: Float!
  insightsByModel: [ModelInsightSummary!]!
}
```

---

## Implementation Priority & Effort Estimates

| Phase | Effort | Dependencies | Impact |
|---|---|---|---|
| **Phase 1: Insight Data Model** | 2–3 days | None | Foundation for everything |
| **Phase 2: Alpha Models** | 3–4 days | Phase 1 | Signal evaluation, ensemble capability |
| **Phase 3: Portfolio Construction** | 2–3 days | Phase 1 | Multi-asset support, proper sizing |
| **Phase 4: Risk Management** | 2–3 days | Phase 3 | Systematic risk control |
| **Phase 5: Framework Engine + UI** | 4–5 days | All above | Full pipeline, dashboard |

**Total: ~14–18 days of focused work**

---

## Design Principles

1. **Additive, not destructive.** The existing `BacktestEngine` and `Strategy` base class continue to work as-is. The framework is a parallel path, not a replacement. Strategies can opt into the framework pipeline or stay classic.

2. **Port the interface, not the implementation.** LEAN's C# classes have a lot of machinery for cloud execution, live trading, serialization, and .NET-specific patterns. We port the _contracts_ (what methods exist, what data flows) and write lightweight Python implementations.

3. **Keep Decimal precision.** Our bit-exact LEAN parity depends on Decimal arithmetic. All new framework code must use Decimal for prices and quantities.

4. **Test against LEAN reference output.** Each Alpha Model port should be validated by confirming it emits the same insights at the same times as a LEAN backtest with `EmitInsight` calls added to the equivalent C# algorithm.

5. **Multi-symbol before multi-asset.** Phase 5 should unlock multi-symbol (e.g., SPY + QQQ), but multi-asset (equities + options in one strategy) is a separate future effort.

---

## Recommended Starting Point

**Start with Phase 1 (Insight data model) immediately.** It's self-contained, has no dependencies, and unblocks everything else. The Insight class can be written and tested in isolation — just create insights, check expiry, and verify scoring math against LEAN's InsightScore.cs behavior.

Then move to Phase 2 (Alpha Models) by refactoring the `SpyEmaCrossoverAlgorithm` to emit insights alongside its existing `set_holdings()` calls. This proves the concept without changing any existing behavior — the strategy still trades the same way, but now it also records structured predictions we can score after the fact.
