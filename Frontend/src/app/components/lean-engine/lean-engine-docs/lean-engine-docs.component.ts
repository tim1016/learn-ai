import { ChangeDetectionStrategy, Component } from "@angular/core";
import { CommonModule } from "@angular/common";
import { RouterModule } from "@angular/router";
import { AccordionModule } from "primeng/accordion";
import { DividerModule } from "primeng/divider";
import { KatexDirective } from "../../../shared/katex.directive";

interface PipelineStep {
  n: number;
  label: string;
  module: string;
  detail: string;
}

interface FormulaDoc {
  label: string;
  formulaLatex: string;
  note: string;
  variablesLatex?: string[];
  codeRef?: string;
}

interface StatisticDoc {
  name: string;
  formulaLatex: string;
  description: string;
  codeRef: string;
  notes?: string;
}

interface FillModeDoc {
  mode: string;
  displayName: string;
  description: string;
  invariant: string;
}

interface InvariantDoc {
  label: string;
  assertion: string;
  why: string;
  testRef: string;
}

interface WorkedExampleRow {
  label: string;
  time: string;
  value: string;
  note: string;
}

interface GlossaryEntry {
  term: string;
  definition: string;
}

/**
 * Engine documentation page. Walks through the data pipeline, indicator
 * math, statistics formulas, fill models, bit-exact invariants, a worked
 * example of the SPY first trade, and a glossary. Formulas render via
 * the shared {@link KatexDirective}.
 */
@Component({
  selector: "app-lean-engine-docs",
  standalone: true,
  imports: [
    CommonModule,
    RouterModule,
    AccordionModule,
    DividerModule,
    KatexDirective,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./lean-engine-docs.component.html",
  styleUrls: ["./lean-engine-docs.component.scss"],
})
export class LeanEngineDocsComponent {
  // ------------------------------------------------------------------
  // Pipeline walkthrough
  // ------------------------------------------------------------------
  readonly pipelineSteps: PipelineStep[] = [
    {
      n: 1,
      label: "Read minute bars",
      module: "engine/data/lean_format.py",
      detail:
        "LeanMinuteDataReader iterates zipped LEAN minute CSVs for the " +
        "symbol and date range. Prices are decoded as Decimal (not float) " +
        "to preserve LEAN's exact arithmetic. Each row becomes a TradeBar " +
        "with OHLCV and Eastern-time start/end_time.",
    },
    {
      n: 2,
      label: "Consolidate to strategy resolution",
      module: "engine/consolidators/trade_bar_consolidator.py",
      detail:
        "TradeBarConsolidator aggregates minute bars into the strategy's " +
        "resolution (15 minutes for SPY) using LEAN's wall-clock alignment " +
        "rules — the first bar of each window snaps to the nearest boundary " +
        "below, and open/high/low/close/volume are assembled with LEAN " +
        "rounding conventions. Only consolidated bars reach the strategy.",
    },
    {
      n: 3,
      label: "Update indicators",
      module: "engine/indicators/",
      detail:
        "Each consolidated bar's close feeds SMA / EMA / Wilders RSI. " +
        "Indicators use Decimal arithmetic throughout and mirror LEAN's " +
        "warm-up behavior bit-exactly (EMA seeded from an internal SMA; " +
        "RSI ready at period+1 samples).",
    },
    {
      n: 4,
      label: "Run strategy decision",
      module: "engine/strategy/algorithms/",
      detail:
        "The strategy inspects indicator values and prior-bar state, then " +
        "optionally calls ctx.set_holdings(symbol, 1) or ctx.liquidate(). " +
        "A _pending_entry snapshot is captured at signal time so indicator " +
        "values reflect the decision that triggered the order, not fill time.",
    },
    {
      n: 5,
      label: "Apply fill model",
      module: "engine/execution/fill_model.py",
      detail:
        "FillModel converts orders into OrderEvents at the chosen price. " +
        "signal_bar_close fills at the signal bar's close (LEAN default); " +
        "next_bar_open defers fills to the next bar's open. Fills feed " +
        "the portfolio and emit on_order_event to the strategy.",
    },
    {
      n: 6,
      label: "Update portfolio",
      module: "engine/execution/portfolio.py",
      detail:
        "Portfolio holds cash, positions, and realized/unrealized P&L. " +
        "Fees of $1.00 per order are charged to cash on each fill. " +
        "final_equity = cash + sum(holdings at last bar's close).",
    },
    {
      n: 7,
      label: "Log trades",
      module: "engine/strategy/base.py (LoggedTrade)",
      detail:
        "on_order_event pairs entry fills with _pending_entry to start an " +
        "_open_trade, and on exit fills, appends a LoggedTrade with the " +
        "indicator snapshot captured at signal time. Trade log entries " +
        "always reflect the portfolio's actual fill prices.",
    },
    {
      n: 8,
      label: "Compute statistics",
      module: "engine/results/statistics.py",
      detail:
        "compute_trade_statistics computes per-trade metrics (win rate, " +
        "profit factor, expectancy). compute_portfolio_statistics rebuilds " +
        "an all-in equity curve from per-trade PnL percentages and derives " +
        "max drawdown plus annualized Sharpe / Sortino / Calmar.",
    },
    {
      n: 9,
      label: "Serialize response",
      module: "routers/engine.py",
      detail:
        "The router converts LoggedTrade into EngineTradeResponse with an " +
        "indicators dict (per-strategy keys) and signal_reason, then wraps " +
        "the summary stats and trade list into EngineBacktestResponse for " +
        "the /api/engine/backtest endpoint.",
    },
  ];

  // ------------------------------------------------------------------
  // Indicator math
  // ------------------------------------------------------------------
  readonly smaFormula: FormulaDoc = {
    label: "Simple Moving Average",
    formulaLatex:
      "\\text{SMA}_n(t) = \\frac{1}{n}\\sum_{i=0}^{n-1} C_{t-i}",
    note:
      "During warm-up (fewer than n samples seen), LEAN — and this engine — " +
      "return the mean of all samples received so far. is_ready flips to " +
      "true once exactly n samples have been observed.",
    variablesLatex: [
      "n = \\text{period length (e.g. } 10 \\text{ for SMA(10))}",
      "C_{t-i} = \\text{close price at bar } t-i",
    ],
    codeRef: "engine/indicators/sma.py",
  };

  readonly emaFormula: FormulaDoc = {
    label: "Exponential Moving Average (SMA-seeded)",
    formulaLatex:
      "\\text{EMA}_n(t) = \\begin{cases} " +
      "\\text{SMA}_n(t) & \\text{if samples} \\le n \\\\[4pt] " +
      "k \\cdot C_t + (1-k) \\cdot \\text{EMA}_n(t-1) & \\text{otherwise} " +
      "\\end{cases}",
    note:
      "The smoothing constant k is fixed at construction. During warm-up " +
      "(samples ≤ n) the value is taken from an internal SimpleMovingAverage " +
      "so that at sample n the EMA exactly equals the SMA of the first n " +
      "closes — this matches LEAN's Indicators/ExponentialMovingAverage.cs " +
      "and is load-bearing for bit-exact reproduction.",
    variablesLatex: [
      "k = \\dfrac{2}{n+1} \\text{ (smoothing factor)}",
      "n = \\text{EMA period (e.g. 5, 10)}",
      "C_t = \\text{current bar close}",
      "\\text{EMA}_n(t-1) = \\text{previous EMA value}",
    ],
    codeRef: "engine/indicators/ema.py",
  };

  readonly rsiFormulas: FormulaDoc[] = [
    {
      label: "Per-bar gain and loss",
      formulaLatex:
        "G_t = \\max(0, C_t - C_{t-1}), \\quad L_t = \\max(0, C_{t-1} - C_t)",
      note:
        "Equality (Cₜ = Cₜ₋₁) is classified as a gain of 0, not a loss, " +
        "matching LEAN's convention.",
    },
    {
      label: "Wilders seeded averages (first n deltas)",
      formulaLatex:
        "\\overline{G}_n = \\frac{1}{n}\\sum_{i=1}^{n} G_i, \\quad " +
        "\\overline{L}_n = \\frac{1}{n}\\sum_{i=1}^{n} L_i",
      note:
        "Initial averages are simple means of the first n gain/loss samples. " +
        "Note that RSI(n) needs n+1 closes to produce n deltas, so is_ready " +
        "flips to true at sample n+1 rather than n.",
    },
    {
      label: "Wilders smoothing (every bar after seed)",
      formulaLatex:
        "\\overline{G}_t = \\frac{\\overline{G}_{t-1} \\cdot (n-1) + G_t}{n}, " +
        "\\quad " +
        "\\overline{L}_t = \\frac{\\overline{L}_{t-1} \\cdot (n-1) + L_t}{n}",
      note:
        "Wilders recursive smoothing — equivalent to an EMA with α = 1/n, " +
        "but historically reported separately.",
    },
    {
      label: "Relative Strength and RSI",
      formulaLatex:
        "\\text{RS}_t = \\frac{\\overline{G}_t}{\\overline{L}_t}, \\quad " +
        "\\text{RSI}_t = 100 - \\frac{100}{1 + \\text{RS}_t}",
      note:
        "Edge case: if round(avg_loss, 10) equals 0, RSI is clamped to 100. " +
        "This precisely reproduces LEAN's zero-division guard.",
      variablesLatex: [
        "n = \\text{RSI period (14 for the SPY strategy)}",
        "\\overline{G}_t, \\overline{L}_t = \\text{smoothed gain/loss averages}",
      ],
      codeRef: "engine/indicators/rsi.py",
    },
  ];

  // ------------------------------------------------------------------
  // Portfolio statistics
  // ------------------------------------------------------------------
  readonly statistics: StatisticDoc[] = [
    {
      name: "Win rate",
      formulaLatex:
        "\\text{Win Rate} = \\frac{N_{\\text{wins}}}{N_{\\text{trades}}}",
      description:
        "Fraction of trades with strictly positive pnl_pct. Trades with " +
        "exactly zero PnL count toward the denominator but not the numerator.",
      codeRef: "compute_trade_statistics()",
    },
    {
      name: "Profit factor",
      formulaLatex:
        "\\text{Profit Factor} = \\frac{\\sum_{t \\in \\text{wins}} |p_t|}" +
        "{\\sum_{t \\in \\text{losses}} |p_t|}",
      description:
        "Gross winning PnL over gross losing PnL, both as absolute values. " +
        "Returns infinity if there are no losing trades, or 0.0 if the " +
        "trade log is empty.",
      codeRef: "compute_trade_statistics()",
    },
    {
      name: "Expectancy",
      formulaLatex:
        "E[\\text{trade}] = \\frac{1}{N}\\sum_{t=1}^{N} p_t",
      description:
        "Average percentage PnL per trade. For a strategy to be profitable, " +
        "expectancy must be positive after fees.",
      codeRef: "compute_trade_statistics()",
    },
    {
      name: "Payoff ratio",
      formulaLatex:
        "\\text{Payoff} = \\frac{\\overline{p}_{\\text{win}}}{|\\overline{p}_{\\text{loss}}|}",
      description:
        "Average winning trade return divided by the absolute value of the " +
        "average losing trade return. A value > 1 means the average winner " +
        "outweighs the average loser.",
      codeRef: "compute_trade_statistics()",
    },
    {
      name: "Max drawdown",
      formulaLatex:
        "\\text{MaxDD} = \\max_{t}\\left( \\frac{\\text{peak}_t - E_t}" +
        "{\\text{peak}_t} \\right), \\quad \\text{peak}_t = \\max_{s \\le t} E_s",
      description:
        "Largest peak-to-trough decline of the rebuilt equity curve, " +
        "returned as a positive fraction. The curve assumes 100% " +
        "allocation per trade (matches SetHoldings(1.0)); leveraged or " +
        "partial strategies need a real equity curve instead.",
      codeRef: "_max_drawdown()",
    },
    {
      name: "Sharpe ratio (annualized)",
      formulaLatex:
        "\\text{Sharpe} = \\frac{\\overline{r}}{\\sigma_r} \\cdot " +
        "\\sqrt{\\text{periods per year}}",
      description:
        "Per-trade returns derived from the rebuilt equity curve, annualized " +
        "using trading_days × 252 / trade_count. Returns None if there are " +
        "fewer than 2 trades or if σᵣ is zero.",
      codeRef: "_sharpe()",
      notes:
        "Sample standard deviation (n−1 denominator), matching LEAN.",
    },
    {
      name: "Sortino ratio (annualized)",
      formulaLatex:
        "\\text{Sortino} = \\frac{\\overline{r}}{\\sigma_d} \\cdot " +
        "\\sqrt{\\text{periods per year}}, \\quad " +
        "\\sigma_d = \\sqrt{\\frac{1}{N}\\sum_{r_t < 0} r_t^2}",
      description:
        "Like Sharpe but uses downside deviation — only negative returns " +
        "contribute to the denominator. Returns None when there are no " +
        "negative returns, since the ratio is meaningless in that case.",
      codeRef: "_sortino()",
    },
    {
      name: "Calmar ratio",
      formulaLatex:
        "\\text{Calmar} = \\frac{\\left(E_{\\text{final}}/E_0\\right)^{1/\\text{years}} - 1}" +
        "{\\text{MaxDD}}",
      description:
        "Compound annual growth rate divided by max drawdown. Requires a " +
        "known trading_days span and positive final equity; otherwise None.",
      codeRef: "compute_portfolio_statistics()",
    },
  ];

  // ------------------------------------------------------------------
  // Fill models
  // ------------------------------------------------------------------
  readonly fillModes: FillModeDoc[] = [
    {
      mode: "signal_bar_close",
      displayName: "Signal bar close (LEAN default)",
      description:
        "The order fills at the close of the bar that triggered the signal. " +
        "This is what LEAN's default market-on-close behavior produces for " +
        "the SPY EMA crossover algorithm.",
      invariant:
        "Preserves bit-exact parity with LEAN's reference log: 63 trades, " +
        "$10,332.98 net profit, $126.00 total fees.",
    },
    {
      mode: "next_bar_open",
      displayName: "Next bar open",
      description:
        "The order fills at the open of the next consolidated bar. This is " +
        "closer to what happens at real brokerage execution, since you " +
        "cannot trade at a bar's close retroactively.",
      invariant:
        "Produces different fill prices but the same trade structure. " +
        "Validated against a snapshot baseline in " +
        "test_spy_next_bar_open_baseline.csv.",
    },
  ];

  // ------------------------------------------------------------------
  // Bit-exact invariants
  // ------------------------------------------------------------------
  readonly invariants: InvariantDoc[] = [
    {
      label: "SPY trade count",
      assertion: "Exactly 63 trades over 2024-03-28 → 2026-03-27.",
      why:
        "Any divergence means a decision-point mismatch — typically a " +
        "consolidator boundary, warm-up off-by-one, or indicator seed bug.",
      testRef: "test_spy_ema_crossover_bit_exact.py",
    },
    {
      label: "Per-trade entry/exit prices",
      assertion: "Match LEAN to 2 decimal places on every trade.",
      why:
        "Price divergence means either the consolidator aggregated the " +
        "wrong minute bars or the fill model rounded differently.",
      testRef: "test_spy_validation.py",
    },
    {
      label: "Indicator snapshots at entry",
      assertion:
        "EMA5/EMA10 match to 4 dp and RSI matches to 2 dp on every trade.",
      why:
        "Catches Decimal-vs-float drift and Wilders-vs-classic RSI " +
        "confusion. This is the most sensitive invariant we assert.",
      testRef: "test_spy_validation.py",
    },
    {
      label: "Total fees",
      assertion: "$126.00 ($1.00 × 2 × 63 trades).",
      why:
        "Sanity check on the portfolio's fee accounting — easy to break " +
        "when refactoring the fill event handler.",
      testRef: "test_spy_ema_crossover_bit_exact.py",
    },
    {
      label: "Net profit",
      assertion: "$10,332.98 over the full window.",
      why:
        "Portfolio-level bottom line. Even if every per-trade value matches, " +
        "an off-by-one in fee application or cash tracking would show up " +
        "here.",
      testRef: "test_spy_ema_crossover_bit_exact.py",
    },
    {
      label: "Cross-engine WIN/LOSS sequence (new strategies)",
      assertion:
        "For each ported strategy, the ordered WIN/LOSS sequence must " +
        "match a hermetic reference implementation on synthetic data.",
      why:
        "Bit-exact LEAN data isn't available for every strategy. The " +
        "ordered WIN/LOSS sequence is the weakest contract that still " +
        "catches signal-logic regressions.",
      testRef: "test_sma_crossover_parity.py",
    },
  ];

  // ------------------------------------------------------------------
  // SPY first trade — worked example
  // ------------------------------------------------------------------
  // Values come directly from fixtures/spy_lean_trades.csv row 1 and
  // match test_spy_ema_crossover_bit_exact.py. Times are America/New_York.
  readonly firstTradeEntry: WorkedExampleRow[] = [
    {
      label: "Signal bar close",
      time: "2024-04-11 12:00",
      value: "$515.34",
      note:
        "15-minute SPY bar ending 12:00 ET. This is the first bar where " +
        "all three indicators are simultaneously ready and the entry " +
        "predicate fires.",
    },
    {
      label: "EMA5",
      time: "2024-04-11 12:00",
      value: "514.1906",
      note:
        "Above EMA10 for the first time since the last time _prev_above " +
        "was false — i.e. a fresh bullish crossover.",
    },
    {
      label: "EMA10",
      time: "2024-04-11 12:00",
      value: "513.9322",
      note:
        "Gap = EMA5 − EMA10 = 0.2584, which clears the 0.20 minimum-gap " +
        "filter (hard-coded in the strategy).",
    },
    {
      label: "RSI14",
      time: "2024-04-11 12:00",
      value: "57.33",
      note:
        "Inside the [50, 70] acceptance band — weak-to-moderate bullish " +
        "momentum without being overbought.",
    },
    {
      label: "Predicate result",
      time: "2024-04-11 12:00",
      value: "ENTRY",
      note:
        "fresh_crossover ∧ gap ≥ 0.20 ∧ 50 ≤ RSI ≤ 70 ⇒ submit " +
        "SetHoldings(SPY, 1.0). _bars_until_exit = 5.",
    },
    {
      label: "Entry fill",
      time: "2024-04-11 12:00",
      value: "$515.34",
      note:
        "signal_bar_close fill model — fills at the signal bar's own close. " +
        "Strategy captures the indicator snapshot into _pending_entry; the " +
        "on_order_event handler promotes it to _open_trade.",
    },
  ];

  readonly firstTradeExit: WorkedExampleRow[] = [
    {
      label: "Bar 1 after entry",
      time: "2024-04-11 12:15",
      value: "—",
      note:
        "_bars_until_exit: 5 → 4. No action taken; position still open.",
    },
    {
      label: "Bar 2",
      time: "2024-04-11 12:30",
      value: "—",
      note: "_bars_until_exit: 4 → 3.",
    },
    {
      label: "Bar 3",
      time: "2024-04-11 12:45",
      value: "—",
      note: "_bars_until_exit: 3 → 2.",
    },
    {
      label: "Bar 4",
      time: "2024-04-11 13:00",
      value: "—",
      note: "_bars_until_exit: 2 → 1.",
    },
    {
      label: "Exit signal bar",
      time: "2024-04-11 13:15",
      value: "$516.97",
      note:
        "_bars_until_exit: 1 → 0. Strategy calls ctx.liquidate(SPY). " +
        "Exit fill price = this bar's close (signal_bar_close mode).",
    },
    {
      label: "Trade result",
      time: "—",
      value: "WIN",
      note:
        "PnL points = 516.97 − 515.34 = 1.63. PnL percent = 1.63 / 515.34 " +
        "= 0.003163 (0.3163%). Appended to trade_log[0] with the " +
        "indicator snapshot from entry.",
    },
  ];

  // ------------------------------------------------------------------
  // Glossary
  // ------------------------------------------------------------------
  readonly glossary: GlossaryEntry[] = [
    {
      term: "TradeBar",
      definition:
        "An OHLCV bar with explicit start_time and end_time. All prices " +
        "are Decimal. The engine never touches raw float price data.",
    },
    {
      term: "LoggedTrade",
      definition:
        "Closed trade record with entry/exit times and prices, PnL, result " +
        "(WIN/LOSS), an indicators dict keyed by strategy-specific names, " +
        "and a signal_reason string. Shared dataclass in strategy/base.py.",
    },
    {
      term: "_pending_entry",
      definition:
        "Short-lived field holding the indicator snapshot between entry " +
        "signal and entry fill. Consumed by on_order_event when the LONG " +
        "fill arrives.",
    },
    {
      term: "_open_trade",
      definition:
        "An entry that has filled but not yet exited. Carries the " +
        "indicator snapshot forward until the exit fill.",
    },
    {
      term: "Fill event",
      definition:
        "OrderEvent emitted by the fill model when an order is executed. " +
        "Drives on_order_event, which is where trade log entries are " +
        "created — never from the bar handler.",
    },
    {
      term: "Strategy registration",
      definition:
        "Entry in _STRATEGY_REGISTRY mapping a strategy name to its " +
        "display metadata, Pydantic parameter schema, and build callable. " +
        "Powers GET /api/engine/strategies.",
    },
    {
      term: "signal_bar_close",
      definition:
        "Fill mode where orders fill at the close of the bar that " +
        "triggered them. LEAN's default; required for bit-exact parity.",
    },
    {
      term: "next_bar_open",
      definition:
        "Fill mode where orders fill at the open of the bar after the " +
        "signal bar. More realistic for live trading simulation; " +
        "validated against a snapshot baseline.",
    },
    {
      term: "EngineVersion",
      definition:
        "(Planned — Phase 3) Monotonic integer identifier for a specific " +
        "engine build. Allows comparing results across engine changes " +
        "without mixing them.",
    },
  ];
}
