import {
  Component, input, signal,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Tooltip } from 'primeng/tooltip';
import type { LeanStatistics } from '../engine-results/engine-results.component';

interface StatItem {
  label: string;
  value: string;
  tooltip: string;
  cssClass?: string;  // 'positive' | 'negative' | ''
  source: string;     // LEAN C# source reference
}

@Component({
  selector: 'app-lean-statistics',
  standalone: true,
  imports: [CommonModule, Tooltip],
  templateUrl: './lean-statistics.component.html',
  styleUrls: ['./lean-statistics.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanStatisticsComponent {
  stats = input.required<LeanStatistics>();

  showFullStats = signal(false);

  // ── Formatting helpers ──

  pct(v: number): string {
    return (v * 100).toFixed(2) + '%';
  }

  dollar(v: number): string {
    return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  ratio(v: number): string {
    return v.toFixed(4);
  }

  days(v: number): string {
    return v + ' days';
  }

  date(ms: number | null): string {
    if (ms == null) return '—';
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
    }).format(ms);
  }

  valClass(v: number): string {
    if (v > 0.0001) return 'positive';
    if (v < -0.0001) return 'negative';
    return '';
  }

  // ── Section builders ──

  get returnMetrics(): StatItem[] {
    const p = this.stats().portfolio;
    return [
      { label: 'Net Profit', value: this.pct(p.total_net_profit), tooltip: '(EndEquity / StartEquity) - 1', cssClass: this.valClass(p.total_net_profit), source: 'PS.cs 100' },
      { label: 'CAGR', value: this.pct(p.compounding_annual_return), tooltip: '(End/Start)^(1/years) - 1, years = calendar days / 365', cssClass: this.valClass(p.compounding_annual_return), source: 'PS.cs 88, S.cs 38' },
      { label: 'Start Equity', value: this.dollar(p.start_equity), tooltip: 'Portfolio value at first bar', cssClass: '', source: 'PS.cs 75' },
      { label: 'End Equity', value: this.dollar(p.end_equity), tooltip: 'Portfolio value at last bar', cssClass: this.valClass(p.end_equity - p.start_equity), source: 'PS.cs 81' },
    ];
  }

  get riskMetrics(): StatItem[] {
    const p = this.stats().portfolio;
    return [
      { label: 'Sharpe Ratio', value: this.ratio(p.sharpe_ratio), tooltip: 'LEAN native: (annual return − LEAN primary-credit risk-free rate) / annual standard deviation. A differently named platform KPI may use a different rate contract.', cssClass: this.valClass(p.sharpe_ratio), source: 'PS.cs 107, S.cs 148' },
      { label: 'Sortino Ratio', value: this.ratio(p.sortino_ratio), tooltip: 'LEAN native: annual excess return over LEAN’s primary-credit rate divided by downside deviation. This is not interchangeable with a Treasury/FRED definition.', cssClass: this.valClass(p.sortino_ratio), source: 'PS.cs 122, S.cs 188' },
      { label: 'PSR', value: this.pct(p.probabilistic_sharpe_ratio), tooltip: 'Probabilistic Sharpe Ratio — Bayesian confidence the true Sharpe > benchmark (Lopez de Prado 2012)', cssClass: this.valClass(p.probabilistic_sharpe_ratio - 0.5), source: 'PS.cs 115, S.cs 199' },
      { label: 'Annual Std Dev', value: this.pct(p.annual_standard_deviation), tooltip: '√(Var(daily returns) × 252)', cssClass: '', source: 'PS.cs 140, S.cs 85' },
      { label: 'Annual Variance', value: this.ratio(p.annual_variance), tooltip: 'Var(daily returns) × 252', cssClass: '', source: 'PS.cs 146, S.cs 69' },
      { label: 'VaR 99%', value: this.pct(p.value_at_risk_99), tooltip: 'Daily return at 1% tail, normal assumption, last 252 days', cssClass: 'negative', source: 'PS.cs 179' },
      { label: 'VaR 95%', value: this.pct(p.value_at_risk_95), tooltip: 'Daily return at 5% tail, normal assumption, last 252 days', cssClass: 'negative', source: 'PS.cs 186' },
    ];
  }

  get drawdownMetrics(): StatItem[] {
    const p = this.stats().portfolio;
    return [
      { label: 'Max Drawdown', value: this.pct(p.drawdown), tooltip: 'Largest peak-to-trough decline in equity curve', cssClass: 'negative', source: 'PS.cs 94, S.cs 261' },
      { label: 'Recovery', value: this.days(p.drawdown_recovery), tooltip: 'Longest calendar days from peak to new peak', cssClass: p.drawdown_recovery > 60 ? 'negative' : '', source: 'PS.cs 192' },
    ];
  }

  get benchmarkMetrics(): StatItem[] {
    const p = this.stats().portfolio;
    return [
      { label: 'Alpha', value: this.ratio(p.alpha), tooltip: 'Excess return vs. benchmark (CAPM). Zero when benchmark is flat.', cssClass: this.valClass(p.alpha), source: 'PS.cs 128' },
      { label: 'Beta', value: this.ratio(p.beta), tooltip: 'Cov(strategy, bench) / Var(bench). Zero when benchmark is flat.', cssClass: '', source: 'PS.cs 134' },
      { label: 'Information Ratio', value: this.ratio(p.information_ratio), tooltip: '(AnnReturn - BenchReturn) / TrackingError', cssClass: this.valClass(p.information_ratio), source: 'PS.cs 153' },
      { label: 'Tracking Error', value: this.pct(p.tracking_error), tooltip: '√(Var(strategy - bench) × 252)', cssClass: '', source: 'PS.cs 160, S.cs 123' },
      { label: 'Treynor Ratio', value: this.ratio(p.treynor_ratio), tooltip: '(AnnReturn - RFR) / Beta. Zero when Beta=0.', cssClass: this.valClass(p.treynor_ratio), source: 'PS.cs 166' },
    ];
  }

  get tradeMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'Total P&L', value: this.dollar(t.total_profit_loss), tooltip: 'Sum of all trade P&L in points', cssClass: this.valClass(t.total_profit_loss), source: 'TS.cs 57' },
      { label: 'Total Profit', value: this.dollar(t.total_profit), tooltip: 'Sum of winning trade P&L', cssClass: 'positive', source: 'TS.cs 63' },
      { label: 'Total Loss', value: this.dollar(t.total_loss), tooltip: 'Sum of losing trade P&L', cssClass: 'negative', source: 'TS.cs 69' },
      { label: 'Largest Win', value: this.dollar(t.largest_profit), tooltip: 'Single best trade', cssClass: 'positive', source: 'TS.cs 75' },
      { label: 'Largest Loss', value: this.dollar(t.largest_loss), tooltip: 'Single worst trade', cssClass: 'negative', source: 'TS.cs 81' },
      { label: 'Avg P&L', value: this.dollar(t.average_profit_loss), tooltip: 'Mean trade P&L', cssClass: this.valClass(t.average_profit_loss), source: 'TS.cs 87' },
      { label: 'Avg Win', value: this.dollar(t.average_profit), tooltip: 'Mean winning trade P&L', cssClass: 'positive', source: 'TS.cs 93' },
      { label: 'Avg Loss', value: this.dollar(t.average_loss), tooltip: 'Mean losing trade P&L', cssClass: 'negative', source: 'TS.cs 99' },
      { label: 'Profit Factor', value: this.ratio(t.profit_factor), tooltip: '|TotalProfit / TotalLoss|', cssClass: this.valClass(t.profit_factor - 1), source: 'TS.cs 227' },
      { label: 'Profit/DD Ratio', value: this.ratio(t.profit_to_max_drawdown_ratio), tooltip: 'TotalNetProfit / MaxDrawdown', cssClass: this.valClass(t.profit_to_max_drawdown_ratio), source: 'TS.cs 247' },
    ];
  }

  get streakMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'Max Consecutive Wins', value: String(t.max_consecutive_winning_trades), tooltip: 'Longest winning streak', cssClass: 'positive', source: 'TS.cs 134' },
      { label: 'Max Consecutive Losses', value: String(t.max_consecutive_losing_trades), tooltip: 'Longest losing streak', cssClass: 'negative', source: 'TS.cs 139' },
      { label: 'Avg Duration', value: t.average_trade_duration, tooltip: 'Average time from entry to exit across all trades', cssClass: '', source: 'TS.cs 104' },
      { label: 'Avg Win Duration', value: t.average_winning_trade_duration, tooltip: 'Average time for winning trades', cssClass: '', source: 'TS.cs 109' },
      { label: 'Avg Loss Duration', value: t.average_losing_trade_duration, tooltip: 'Average time for losing trades', cssClass: '', source: 'TS.cs 114' },
    ];
  }

  get distributionMetrics(): StatItem[] {
    const t = this.stats().trade;
    const p = this.stats().portfolio;
    return [
      { label: 'P&L Std Dev', value: this.dollar(t.profit_loss_standard_deviation), tooltip: 'Sample standard deviation of closed-trade profit/loss in account currency', cssClass: '', source: 'TS.cs 212' },
      { label: 'Downside Dev', value: this.dollar(t.profit_loss_downside_deviation), tooltip: 'Sample standard deviation of losing-trade profit/loss in account currency', cssClass: '', source: 'TS.cs 219' },
      { label: 'Trade Sharpe', value: this.ratio(t.sharpe_ratio), tooltip: 'Mean closed-trade P&L divided by its sample standard deviation; LEAN does not annualize this trade statistic', cssClass: this.valClass(t.sharpe_ratio), source: 'TS.cs 233' },
      { label: 'Trade Sortino', value: this.ratio(t.sortino_ratio), tooltip: 'Mean closed-trade P&L divided by losing-trade downside deviation; LEAN does not annualize this trade statistic', cssClass: this.valClass(t.sortino_ratio), source: 'TS.cs 239' },
      { label: 'Avg Win Rate', value: this.pct(p.average_win_rate), tooltip: 'Capital-normalized average winning return (LEAN GetProfitLossRates)', cssClass: 'positive', source: 'PS.cs 36' },
      { label: 'Avg Loss Rate', value: this.pct(p.average_loss_rate), tooltip: 'Capital-normalized average losing return (LEAN GetProfitLossRates)', cssClass: 'negative', source: 'PS.cs 42' },
      { label: 'LEAN Expectancy', value: this.ratio(p.expectancy), tooltip: 'WinRate × ProfitLossRatio - LossRate (LEAN formula)', cssClass: this.valClass(p.expectancy), source: 'PS.cs 69' },
      { label: 'Profit/Loss Ratio', value: this.ratio(p.profit_loss_ratio), tooltip: 'LEAN portfolio average-win rate divided by the absolute average-loss rate', cssClass: this.valClass(p.profit_loss_ratio - 1), source: 'PS.cs 48' },
      { label: 'Win Rate', value: this.pct(p.win_rate), tooltip: 'Winning observations divided by all non-zero observations', cssClass: this.valClass(p.win_rate - 0.5), source: 'PS.cs 54' },
      { label: 'Loss Rate', value: this.pct(p.loss_rate), tooltip: 'Losing observations divided by all non-zero observations', cssClass: this.valClass(0.5 - p.loss_rate), source: 'PS.cs 60' },
      { label: 'Portfolio Turnover', value: this.pct(p.portfolio_turnover), tooltip: 'Average absolute daily holdings turnover', cssClass: '', source: 'PS.cs 198' },
    ];
  }

  get tradePopulationMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'First Entry', value: this.date(t.start_date_time), tooltip: 'Entry timestamp of the first closed trade, displayed in your local timezone', source: 'TS.cs 33' },
      { label: 'Last Exit', value: this.date(t.end_date_time), tooltip: 'Exit timestamp of the final closed trade, displayed in your local timezone', source: 'TS.cs 39' },
      { label: 'Closed Trades', value: String(t.total_number_of_trades), tooltip: 'All closed trades supplied to LEAN TradeStatistics', source: 'TS.cs 45' },
      { label: 'Winning Trades', value: String(t.number_of_winning_trades), tooltip: 'Closed trades with positive profit/loss', cssClass: 'positive', source: 'TS.cs 51' },
      { label: 'Losing Trades', value: String(t.number_of_losing_trades), tooltip: 'Closed trades with negative profit/loss', cssClass: 'negative', source: 'TS.cs 57' },
      { label: 'Trade Win Rate', value: this.pct(t.win_rate), tooltip: 'Winning trades divided by all closed trades', cssClass: this.valClass(t.win_rate - 0.5), source: 'TS.cs 164' },
      { label: 'Trade Loss Rate', value: this.pct(t.loss_rate), tooltip: 'Losing trades divided by all closed trades', cssClass: this.valClass(0.5 - t.loss_rate), source: 'TS.cs 170' },
      { label: 'Win/Loss Count Ratio', value: this.ratio(t.win_loss_ratio), tooltip: 'Number of winning trades divided by losing trades', cssClass: this.valClass(t.win_loss_ratio - 1), source: 'TS.cs 158' },
      { label: 'Profit/Loss Ratio', value: this.ratio(t.profit_loss_ratio), tooltip: 'Average winning return divided by the absolute average losing return', cssClass: this.valClass(t.profit_loss_ratio - 1), source: 'TS.cs 152' },
    ];
  }

  get tradeExcursionMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'Average MAE', value: this.dollar(t.average_mae), tooltip: 'Average maximum adverse excursion in account currency across closed trades', cssClass: 'negative', source: 'TS.cs 176' },
      { label: 'Average MFE', value: this.dollar(t.average_mfe), tooltip: 'Average maximum favorable excursion in account currency across closed trades', cssClass: 'positive', source: 'TS.cs 182' },
      { label: 'Largest MAE', value: this.dollar(t.largest_mae), tooltip: 'Largest maximum adverse excursion in account currency', cssClass: 'negative', source: 'TS.cs 188' },
      { label: 'Largest MFE', value: this.dollar(t.largest_mfe), tooltip: 'Largest maximum favorable excursion in account currency', cssClass: 'positive', source: 'TS.cs 194' },
      { label: 'Max Closed Drawdown', value: this.dollar(t.maximum_closed_trade_drawdown), tooltip: 'Largest peak-to-trough drawdown measured at closed-trade boundaries', cssClass: 'negative', source: 'TS.cs 200' },
      { label: 'Max Intra-trade Drawdown', value: this.dollar(t.maximum_intra_trade_drawdown), tooltip: 'Largest peak-to-trough drawdown including movement inside a trade', cssClass: 'negative', source: 'TS.cs 206' },
      { label: 'Max End Drawdown', value: this.dollar(t.maximum_end_trade_drawdown), tooltip: 'Largest drawdown observed at a trade exit', cssClass: 'negative', source: 'TS.cs 253' },
      { label: 'Average End Drawdown', value: this.dollar(t.average_end_trade_drawdown), tooltip: 'Average drawdown observed at trade exits', cssClass: 'negative', source: 'TS.cs 259' },
    ];
  }

  get durationMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      ...this.streakMetrics,
      { label: 'Median Duration', value: t.median_trade_duration, tooltip: 'Median entry-to-exit duration across all closed trades', source: 'TS.cs 119' },
      { label: 'Median Win Duration', value: t.median_winning_trade_duration, tooltip: 'Median entry-to-exit duration for winning trades', source: 'TS.cs 124' },
      { label: 'Median Loss Duration', value: t.median_losing_trade_duration, tooltip: 'Median entry-to-exit duration for losing trades', source: 'TS.cs 129' },
      { label: 'Max Drawdown Duration', value: t.maximum_drawdown_duration, tooltip: 'Longest closed-trade equity drawdown interval', cssClass: 'negative', source: 'TS.cs 265' },
    ];
  }

  get runtimeMetrics(): StatItem[] {
    const r = this.stats().runtime;
    return [
      { label: 'Equity', value: this.dollar(r.equity), tooltip: 'LEAN runtime portfolio equity at completion', cssClass: this.valClass(r.net_profit), source: 'Result.RuntimeStatistics' },
      { label: 'Holdings', value: this.dollar(r.holdings), tooltip: 'LEAN runtime holdings value at completion', source: 'Result.RuntimeStatistics' },
      { label: 'Net Profit', value: this.dollar(r.net_profit), tooltip: 'LEAN runtime net profit', cssClass: this.valClass(r.net_profit), source: 'Result.RuntimeStatistics' },
      { label: 'Total Return', value: this.pct(r.total_return), tooltip: 'LEAN runtime total return', cssClass: this.valClass(r.total_return), source: 'Result.RuntimeStatistics' },
      { label: 'Unrealized', value: this.dollar(r.unrealized), tooltip: 'LEAN runtime unrealized profit/loss', cssClass: this.valClass(r.unrealized), source: 'Result.RuntimeStatistics' },
      { label: 'Fees', value: this.dollar(r.fees), tooltip: 'Native LEAN runtime fee value; its sign is retained exactly as emitted', cssClass: this.valClass(r.fees), source: 'Result.RuntimeStatistics' },
      { label: 'Volume', value: this.dollar(r.volume), tooltip: 'Native LEAN runtime traded volume', source: 'Result.RuntimeStatistics' },
      { label: 'Orders', value: String(r.total_orders), tooltip: 'Native LEAN runtime order count', source: 'Result.RuntimeStatistics' },
      { label: 'Runtime PSR', value: this.pct(r.probabilistic_sharpe_ratio), tooltip: 'Native LEAN runtime probabilistic Sharpe ratio', cssClass: this.valClass(r.probabilistic_sharpe_ratio - 0.5), source: 'Result.RuntimeStatistics' },
      { label: 'Trade Fees', value: this.dollar(this.stats().trade.total_fees), tooltip: 'Total fees attributed by LEAN TradeStatistics', cssClass: 'negative', source: 'TS.cs 271' },
    ];
  }
}
