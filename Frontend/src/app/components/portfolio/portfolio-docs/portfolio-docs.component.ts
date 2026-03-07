import { Component, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Accordion, AccordionContent, AccordionHeader, AccordionPanel } from 'primeng/accordion';
import { KatexDirective } from '../../../shared/katex.directive';

interface FormulaDoc {
  name: string;
  formulaLatex: string;
  variablesLatex: string[];
  interpretation: string;
}

interface RiskRuleDoc {
  ruleType: string;
  computation: string;
  example: string;
}

@Component({
  selector: 'app-portfolio-docs',
  standalone: true,
  imports: [CommonModule, Accordion, AccordionContent, AccordionHeader, AccordionPanel, KatexDirective],
  templateUrl: './portfolio-docs.component.html',
  styleUrls: ['./portfolio-docs.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PortfolioDocsComponent {

  // ── Section 1: Architecture Overview ──

  architectureLayers = [
    { name: 'Angular Frontend', detail: '8 tab components communicating via PortfolioService (GraphQL client over HttpClient)' },
    { name: 'Hot Chocolate v15 GraphQL', detail: 'PortfolioQuery + PortfolioMutation resolvers exposing all operations' },
    { name: 'Service Layer', detail: 'PortfolioService, PositionEngine, ValuationService, SnapshotService, RiskService, ReconciliationService, StrategyAttributionService' },
    { name: 'EF Core 10 + PostgreSQL 16', detail: 'Event-sourced trade log with derived position/lot state' },
  ];

  designPrinciples = [
    { title: 'Event Sourcing', description: 'PortfolioTrade records are immutable facts. Positions and lots are derived state rebuilt from the trade log at any time.' },
    { title: 'FIFO Lot Tracking', description: 'Every buy creates a PositionLot. Sells close the oldest open lots first, making realized PnL deterministic and auditable.' },
    { title: 'Multiplier-Aware', description: 'All PnL, market value, and delta calculations multiply by the contract multiplier (1 for stocks, 100 for standard options).' },
    { title: 'Cash Tracking', description: 'Account.Cash is updated on every fill — buys deduct, sells add — including fees.' },
  ];

  // ── Section 2: FIFO Algorithm ──

  fifoFormulas: FormulaDoc[] = [
    {
      name: 'Lot Close Quantity',
      formulaLatex: '\\text{closeQty} = \\min(\\text{lot.RemainingQty},\\; \\text{sellQtyLeft})',
      variablesLatex: [
        '\\text{lot.RemainingQty} = \\text{shares/contracts still open in this lot}',
        '\\text{sellQtyLeft} = \\text{remaining sell quantity to allocate}',
      ],
      interpretation: 'Process lots in FIFO order (oldest first). For each lot, close the smaller of the lot remainder or the unallocated sell quantity.',
    },
    {
      name: 'Per-Lot Realized PnL',
      formulaLatex: '\\text{PnL}_{\\text{lot}} = (P_{\\text{sell}} - P_{\\text{entry}}) \\times Q_{\\text{close}} \\times M',
      variablesLatex: [
        'P_{\\text{sell}} = \\text{execution price of the sell trade}',
        'P_{\\text{entry}} = \\text{entry price of the lot}',
        'Q_{\\text{close}} = \\text{quantity closed from this lot}',
        'M = \\text{contract multiplier (1 for stocks, 100 for options)}',
      ],
      interpretation: 'Realized PnL is computed per lot at close time. The multiplier ensures option PnL reflects the notional value per contract.',
    },
    {
      name: 'Position Realized PnL',
      formulaLatex: '\\text{RealizedPnL}_{\\text{pos}} = \\sum_{i=1}^{n} \\text{PnL}_{\\text{lot}_i}',
      variablesLatex: [
        'n = \\text{total number of lots (open and closed)}',
      ],
      interpretation: 'The position\'s total realized PnL is the sum across all its lots.',
    },
    {
      name: 'Average Cost Basis',
      formulaLatex: '\\bar{C} = \\frac{\\sum_{j \\in \\text{open}} P_{\\text{entry},j} \\times Q_{\\text{rem},j}}{\\sum_{j \\in \\text{open}} Q_{\\text{rem},j}}',
      variablesLatex: [
        'j \\in \\text{open} = \\text{lots with RemainingQuantity} > 0',
        'P_{\\text{entry},j} = \\text{entry price of lot } j',
        'Q_{\\text{rem},j} = \\text{remaining quantity of lot } j',
      ],
      interpretation: 'Weighted average entry price across all open lots. Updates after every sell as lots are fully or partially closed.',
    },
  ];

  // ── Section 3: Valuation Formulas ──

  valuationFormulas: FormulaDoc[] = [
    {
      name: 'Position Market Value',
      formulaLatex: '\\text{MV}_i = P_{\\text{current},i} \\times Q_i \\times M_i',
      variablesLatex: [
        'P_{\\text{current},i} = \\text{current market price}',
        'Q_i = \\text{net quantity (open lots)}',
        'M_i = \\text{contract multiplier}',
      ],
      interpretation: 'The dollar value of a single position at current market prices.',
    },
    {
      name: 'Unrealized PnL',
      formulaLatex: '\\text{UPnL}_i = (P_{\\text{current},i} - \\bar{C}_i) \\times Q_i \\times M_i',
      variablesLatex: [
        '\\bar{C}_i = \\text{average cost basis of position } i',
      ],
      interpretation: 'Paper profit or loss — what you would realize if you closed the position now.',
    },
    {
      name: 'Portfolio Equity',
      formulaLatex: '\\text{Equity} = \\text{Cash} + \\sum_{i=1}^{N} \\text{MV}_i',
      variablesLatex: [
        '\\text{Cash} = \\text{current account cash balance}',
        'N = \\text{number of open positions}',
      ],
      interpretation: 'Total account value: liquid cash plus the market value of all open positions.',
    },
    {
      name: 'Cash Update (Buy)',
      formulaLatex: '\\text{Cash}^\\prime = \\text{Cash} - (P \\times Q \\times M + F)',
      variablesLatex: [
        'P = \\text{fill price},\\quad Q = \\text{quantity},\\quad M = \\text{multiplier},\\quad F = \\text{fees}',
      ],
      interpretation: 'Buying deducts the notional cost plus fees from the cash balance.',
    },
    {
      name: 'Cash Update (Sell)',
      formulaLatex: '\\text{Cash}^\\prime = \\text{Cash} + (P \\times Q \\times M - F)',
      variablesLatex: [
        'P = \\text{fill price},\\quad Q = \\text{quantity},\\quad M = \\text{multiplier},\\quad F = \\text{fees}',
      ],
      interpretation: 'Selling adds the notional proceeds minus fees to the cash balance.',
    },
  ];

  // ── Section 4: Performance Metrics ──

  metricsFormulas: FormulaDoc[] = [
    {
      name: 'Daily Return',
      formulaLatex: 'R_t = \\frac{E_t - E_{t-1}}{E_{t-1}}',
      variablesLatex: [
        'E_t = \\text{equity at snapshot } t',
      ],
      interpretation: 'Simple return between consecutive equity snapshots.',
    },
    {
      name: 'Total Return',
      formulaLatex: 'R_{\\text{total}} = \\frac{E_n - E_0}{E_0} \\times 100\\%',
      variablesLatex: [
        'E_0 = \\text{first snapshot equity},\\quad E_n = \\text{last snapshot equity}',
      ],
      interpretation: 'Cumulative percentage return over the entire snapshot history.',
    },
    {
      name: 'Sharpe Ratio',
      formulaLatex: 'S = \\frac{\\bar{R}}{\\sigma_R} \\times \\sqrt{252}',
      variablesLatex: [
        '\\bar{R} = \\text{mean daily return}',
        '\\sigma_R = \\text{standard deviation of daily returns}',
        '252 = \\text{trading days per year (annualization factor)}',
      ],
      interpretation: 'Risk-adjusted return: how much excess return per unit of total volatility. Higher is better; >1 is generally considered good.',
    },
    {
      name: 'Sortino Ratio',
      formulaLatex: 'S_{\\text{sort}} = \\frac{\\bar{R}}{\\sigma_D} \\times \\sqrt{252}',
      variablesLatex: [
        '\\sigma_D = \\sqrt{\\frac{1}{n}\\sum_{t: R_t < 0} R_t^2} \\quad \\text{(downside deviation)}',
      ],
      interpretation: 'Like Sharpe but only penalizes downside volatility. Preferred when returns are asymmetric.',
    },
    {
      name: 'Maximum Drawdown',
      formulaLatex: '\\text{MDD} = \\max_{t} \\left( \\frac{\\text{Peak}_t - E_t}{\\text{Peak}_t} \\right)',
      variablesLatex: [
        '\\text{Peak}_t = \\max(E_0, E_1, \\ldots, E_t)',
      ],
      interpretation: 'The worst peak-to-trough decline as a percentage. Measures the largest loss from a historical high.',
    },
    {
      name: 'Calmar Ratio',
      formulaLatex: 'C = \\frac{R_{\\text{ann}}}{\\text{MDD}_{\\%}}',
      variablesLatex: [
        'R_{\\text{ann}} = \\text{annualized total return}',
        '\\text{MDD}_{\\%} = \\text{max drawdown percent}',
      ],
      interpretation: 'Return relative to worst drawdown. Higher values indicate better drawdown-adjusted performance.',
    },
    {
      name: 'Win Rate',
      formulaLatex: 'W = \\frac{\\#\\{t : R_t > 0\\}}{\\#\\{t : R_t \\neq 0\\}}',
      variablesLatex: [],
      interpretation: 'Fraction of return periods that were profitable.',
    },
    {
      name: 'Profit Factor',
      formulaLatex: 'PF = \\frac{\\sum_{t: R_t > 0} R_t}{\\left|\\sum_{t: R_t < 0} R_t\\right|}',
      variablesLatex: [],
      interpretation: 'Ratio of gross profits to gross losses. PF > 1 means the system is profitable overall.',
    },
  ];

  // ── Section 5: Risk Formulas ──

  riskFormulas: FormulaDoc[] = [
    {
      name: 'Dollar Delta',
      formulaLatex: '\\$\\Delta_i = \\delta_i \\times P_i \\times Q_i \\times M_i',
      variablesLatex: [
        '\\delta_i = \\text{position delta (1 for stocks, entry delta for options)}',
        'P_i = \\text{current price of the underlying}',
        'Q_i = \\text{quantity},\\quad M_i = \\text{multiplier}',
      ],
      interpretation: 'The dollar change in position value for a 1-unit move in the underlying. Aggregated across all positions gives portfolio dollar delta.',
    },
    {
      name: 'Portfolio Vega',
      formulaLatex: '\\mathcal{V}_{\\text{port}} = \\sum_{i \\in \\text{options}} \\nu_i \\times Q_i \\times M_i',
      variablesLatex: [
        '\\nu_i = \\text{entry vega of option position } i',
      ],
      interpretation: 'Total portfolio sensitivity to a 1% change in implied volatility across all option positions.',
    },
  ];

  riskRules: RiskRuleDoc[] = [
    { ruleType: 'MaxDrawdown', computation: '(peakEquity - currentEquity) / peakEquity', example: 'Threshold 0.10 triggers at 10% drawdown' },
    { ruleType: 'MaxPositionSize', computation: 'max(position MV) / equity', example: 'Threshold 0.25 triggers if any position > 25% of equity' },
    { ruleType: 'MaxVegaExposure', computation: '|portfolio vega|', example: 'Threshold 5000 triggers if absolute vega exceeds $5,000' },
    { ruleType: 'MaxDelta', computation: '|net dollar delta|', example: 'Threshold 100000 triggers if net delta exceeds $100k' },
  ];

  // ── Section 6: Scenario Analysis ──

  scenarioFormulas: FormulaDoc[] = [
    {
      name: 'Price Shock',
      formulaLatex: 'P^\\prime_i = P_i \\times (1 + \\Delta P_{\\%})',
      variablesLatex: [
        '\\Delta P_{\\%} = \\text{price change percent (e.g., -0.10 for -10\\%)}',
      ],
      interpretation: 'All position prices are shifted by the given percentage to simulate a market-wide move.',
    },
    {
      name: 'Vega Impact (IV Shock)',
      formulaLatex: '\\text{VegaImpact}_i = \\nu_i \\times \\Delta\\sigma \\times Q_i \\times M_i',
      variablesLatex: [
        '\\Delta\\sigma = \\text{IV change in percentage points}',
        '\\nu_i = \\text{entry vega of option } i',
      ],
      interpretation: 'Simulates the effect of implied volatility changing by a given amount on each option position.',
    },
    {
      name: 'Theta Decay',
      formulaLatex: '\\text{ThetaImpact}_i = \\theta_i \\times T \\times Q_i \\times M_i',
      variablesLatex: [
        '\\theta_i = \\text{entry theta of option } i',
        'T = \\text{days forward}',
      ],
      interpretation: 'Simulates time decay over T days. Theta is typically negative, so this reduces option values.',
    },
    {
      name: 'Scenario PnL',
      formulaLatex: '\\text{PnL}_{\\text{scenario}} = \\text{Equity}_{\\text{scenario}} - \\text{Equity}_{\\text{current}}',
      variablesLatex: [],
      interpretation: 'The net dollar impact of all combined shocks (price, IV, theta) on the portfolio.',
    },
  ];

  // ── Section 7: Strategy Attribution ──

  attributionFormulas: FormulaDoc[] = [
    {
      name: 'Strategy PnL',
      formulaLatex: '\\text{PnL}_s = \\sum_{t \\in \\text{trades}(s)} \\text{RealizedPnL}_t',
      variablesLatex: [
        's = \\text{a specific strategy execution}',
        '\\text{trades}(s) = \\text{all trades linked to strategy } s',
      ],
      interpretation: 'Total realized PnL from all trades attributed to a given strategy execution.',
    },
    {
      name: 'Contribution Percent',
      formulaLatex: 'C_s = \\frac{\\text{PnL}_s}{\\sum_{k} \\text{PnL}_k} \\times 100\\%',
      variablesLatex: [
        'k = \\text{all strategies with trades in the account}',
      ],
      interpretation: 'Each strategy\'s share of the total attributed PnL. Shows which strategies drive returns.',
    },
    {
      name: 'Strategy Win Rate',
      formulaLatex: 'W_s = \\frac{\\#\\{t \\in \\text{trades}(s) : \\text{PnL}_t > 0\\}}{|\\text{trades}(s)|}',
      variablesLatex: [],
      interpretation: 'Fraction of profitable trades within a single strategy.',
    },
  ];

  // ── Section 8: Reconciliation ──

  reconciliationSteps = [
    { step: 1, action: 'Snapshot', detail: 'Read all current (cached) positions for the account.' },
    { step: 2, action: 'Rebuild', detail: 'Replay every PortfolioTrade through the FIFO engine to produce rebuilt positions.' },
    { step: 3, action: 'Compare', detail: 'For each ticker, diff NetQuantity and RealizedPnL between cached and rebuilt.' },
    { step: 4, action: 'Report', detail: 'Any difference generates a PositionDrift entry with drift type and magnitude.' },
    { step: 5, action: 'Auto-Fix', detail: 'If drift is found, RebuildPositionsAsync replaces cached state with rebuilt state.' },
  ];
}
