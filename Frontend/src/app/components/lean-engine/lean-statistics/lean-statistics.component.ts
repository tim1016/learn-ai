import {
  Component, computed, input, signal, type Signal,
  ChangeDetectionStrategy,
} from '@angular/core';
import type { LeanStatistics } from '../engine-results/engine-results.component';
import { MetricHelpModalComponent } from '../../strategy-lab/metric-help-modal/metric-help-modal.component';

interface StatItem {
  label: string;
  value: string;
  documentationVariantId: string;
  cssClass?: string;  // 'positive' | 'negative' | ''
  source: string;     // LEAN C# source reference
}

interface StatSection {
  label: string;
  iconClass: string;
  gridClass: string;
  metrics: Signal<StatItem[]>;
}

@Component({
  selector: 'app-lean-statistics',
  imports: [MetricHelpModalComponent],
  templateUrl: './lean-statistics.component.html',
  styleUrl: './lean-statistics.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanStatisticsComponent {
  stats = input.required<LeanStatistics>();

  showFullStats = signal(false);

  readonly headlineSections: readonly StatSection[] = [
    { label: 'Returns', iconClass: 'pi pi-chart-line', gridClass: 'kpi-grid kpi-grid--4', metrics: computed(() => this.returnMetrics) },
    { label: 'Risk', iconClass: 'pi pi-shield', gridClass: 'kpi-grid kpi-grid--4', metrics: computed(() => this.riskMetrics) },
    { label: 'Drawdown', iconClass: 'pi pi-arrow-down', gridClass: 'kpi-grid kpi-grid--2', metrics: computed(() => this.drawdownMetrics) },
  ];
  readonly fullSections: readonly StatSection[] = [
    { label: 'Benchmark', iconClass: 'pi pi-compass', gridClass: 'kpi-grid kpi-grid--5', metrics: computed(() => this.benchmarkMetrics) },
    { label: 'Trade Population & Rates', iconClass: 'pi pi-users', gridClass: 'kpi-grid kpi-grid--5', metrics: computed(() => this.tradePopulationMetrics) },
    { label: 'Trades', iconClass: 'pi pi-dollar', gridClass: 'kpi-grid kpi-grid--5', metrics: computed(() => this.tradeMetrics) },
    { label: 'Streaks & Duration', iconClass: 'pi pi-bolt', gridClass: 'kpi-grid kpi-grid--5', metrics: computed(() => this.durationMetrics) },
    { label: 'Excursion & Trade Drawdown', iconClass: 'pi pi-arrows-h', gridClass: 'kpi-grid kpi-grid--4', metrics: computed(() => this.tradeExcursionMetrics) },
    { label: 'Distribution', iconClass: 'pi pi-chart-bar', gridClass: 'kpi-grid kpi-grid--4', metrics: computed(() => this.distributionMetrics) },
    { label: 'Native Runtime Snapshot', iconClass: 'pi pi-cog', gridClass: 'kpi-grid kpi-grid--5', metrics: computed(() => this.runtimeMetrics) },
  ];
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
      { label: 'Net Profit', value: this.pct(p.total_net_profit), documentationVariantId: 'lean_native.portfolio.total_net_profit.v1', cssClass: this.valClass(p.total_net_profit), source: 'PS.cs 100' },
      { label: 'CAGR', value: this.pct(p.compounding_annual_return), documentationVariantId: 'lean_native.portfolio.compounding_annual_return.v1', cssClass: this.valClass(p.compounding_annual_return), source: 'PS.cs 88, S.cs 38' },
      { label: 'Start Equity', value: this.dollar(p.start_equity), documentationVariantId: 'lean_native.portfolio.start_equity.v1', cssClass: '', source: 'PS.cs 75' },
      { label: 'End Equity', value: this.dollar(p.end_equity), documentationVariantId: 'lean_native.portfolio.end_equity.v1', cssClass: this.valClass(p.end_equity - p.start_equity), source: 'PS.cs 81' },
    ];
  }

  get riskMetrics(): StatItem[] {
    const p = this.stats().portfolio;
    return [
      { label: 'Sharpe Ratio', value: this.ratio(p.sharpe_ratio), documentationVariantId: 'sharpe.lean_native.v1', cssClass: this.valClass(p.sharpe_ratio), source: 'PS.cs 107, S.cs 148' },
      { label: 'Sortino Ratio', value: this.ratio(p.sortino_ratio), documentationVariantId: 'lean_native.portfolio.sortino_ratio.v1', cssClass: this.valClass(p.sortino_ratio), source: 'PS.cs 122, S.cs 188' },
      { label: 'PSR', value: this.pct(p.probabilistic_sharpe_ratio), documentationVariantId: 'lean_native.portfolio.probabilistic_sharpe_ratio.v1', cssClass: this.valClass(p.probabilistic_sharpe_ratio - 0.5), source: 'PS.cs 115, S.cs 199' },
      { label: 'Annual Std Dev', value: this.pct(p.annual_standard_deviation), documentationVariantId: 'lean_native.portfolio.annual_standard_deviation.v1', cssClass: '', source: 'PS.cs 140, S.cs 85' },
      { label: 'Annual Variance', value: this.ratio(p.annual_variance), documentationVariantId: 'lean_native.portfolio.annual_variance.v1', cssClass: '', source: 'PS.cs 146, S.cs 69' },
      { label: 'VaR 99%', value: this.pct(p.value_at_risk_99), documentationVariantId: 'lean_native.portfolio.value_at_risk99.v1', cssClass: 'negative', source: 'PS.cs 179' },
      { label: 'VaR 95%', value: this.pct(p.value_at_risk_95), documentationVariantId: 'lean_native.portfolio.value_at_risk95.v1', cssClass: 'negative', source: 'PS.cs 186' },
    ];
  }

  get drawdownMetrics(): StatItem[] {
    const p = this.stats().portfolio;
    return [
      { label: 'Max Drawdown', value: this.pct(p.drawdown), documentationVariantId: 'lean_native.portfolio.drawdown.v1', cssClass: 'negative', source: 'PS.cs 94, S.cs 261' },
      { label: 'Recovery', value: this.days(p.drawdown_recovery), documentationVariantId: 'lean_native.portfolio.drawdown_recovery.v1', cssClass: p.drawdown_recovery > 60 ? 'negative' : '', source: 'PS.cs 192' },
    ];
  }

  get benchmarkMetrics(): StatItem[] {
    const p = this.stats().portfolio;
    return [
      { label: 'Alpha', value: this.ratio(p.alpha), documentationVariantId: 'lean_native.portfolio.alpha.v1', cssClass: this.valClass(p.alpha), source: 'PS.cs 128' },
      { label: 'Beta', value: this.ratio(p.beta), documentationVariantId: 'lean_native.portfolio.beta.v1', cssClass: '', source: 'PS.cs 134' },
      { label: 'Information Ratio', value: this.ratio(p.information_ratio), documentationVariantId: 'lean_native.portfolio.information_ratio.v1', cssClass: this.valClass(p.information_ratio), source: 'PS.cs 153' },
      { label: 'Tracking Error', value: this.pct(p.tracking_error), documentationVariantId: 'lean_native.portfolio.tracking_error.v1', cssClass: '', source: 'PS.cs 160, S.cs 123' },
      { label: 'Treynor Ratio', value: this.ratio(p.treynor_ratio), documentationVariantId: 'lean_native.portfolio.treynor_ratio.v1', cssClass: this.valClass(p.treynor_ratio), source: 'PS.cs 166' },
    ];
  }

  get tradeMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'Total P&L', value: this.dollar(t.total_profit_loss), documentationVariantId: 'lean_native.trade.total_profit_loss.v1', cssClass: this.valClass(t.total_profit_loss), source: 'TS.cs 57' },
      { label: 'Total Profit', value: this.dollar(t.total_profit), documentationVariantId: 'lean_native.trade.total_profit.v1', cssClass: 'positive', source: 'TS.cs 63' },
      { label: 'Total Loss', value: this.dollar(t.total_loss), documentationVariantId: 'lean_native.trade.total_loss.v1', cssClass: 'negative', source: 'TS.cs 69' },
      { label: 'Largest Win', value: this.dollar(t.largest_profit), documentationVariantId: 'lean_native.trade.largest_profit.v1', cssClass: 'positive', source: 'TS.cs 75' },
      { label: 'Largest Loss', value: this.dollar(t.largest_loss), documentationVariantId: 'lean_native.trade.largest_loss.v1', cssClass: 'negative', source: 'TS.cs 81' },
      { label: 'Avg P&L', value: this.dollar(t.average_profit_loss), documentationVariantId: 'lean_native.trade.average_profit_loss.v1', cssClass: this.valClass(t.average_profit_loss), source: 'TS.cs 87' },
      { label: 'Avg Win', value: this.dollar(t.average_profit), documentationVariantId: 'lean_native.trade.average_profit.v1', cssClass: 'positive', source: 'TS.cs 93' },
      { label: 'Avg Loss', value: this.dollar(t.average_loss), documentationVariantId: 'lean_native.trade.average_loss.v1', cssClass: 'negative', source: 'TS.cs 99' },
      { label: 'Profit Factor', value: this.ratio(t.profit_factor), documentationVariantId: 'lean_native.trade.profit_factor.v1', cssClass: this.valClass(t.profit_factor - 1), source: 'TS.cs 227' },
      { label: 'Profit/DD Ratio', value: this.ratio(t.profit_to_max_drawdown_ratio), documentationVariantId: 'lean_native.trade.profit_to_max_drawdown_ratio.v1', cssClass: this.valClass(t.profit_to_max_drawdown_ratio), source: 'TS.cs 247' },
    ];
  }

  get streakMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'Max Consecutive Wins', value: String(t.max_consecutive_winning_trades), documentationVariantId: 'lean_native.trade.max_consecutive_winning_trades.v1', cssClass: 'positive', source: 'TS.cs 134' },
      { label: 'Max Consecutive Losses', value: String(t.max_consecutive_losing_trades), documentationVariantId: 'lean_native.trade.max_consecutive_losing_trades.v1', cssClass: 'negative', source: 'TS.cs 139' },
      { label: 'Avg Duration', value: t.average_trade_duration, documentationVariantId: 'lean_native.trade.average_trade_duration.v1', cssClass: '', source: 'TS.cs 104' },
      { label: 'Avg Win Duration', value: t.average_winning_trade_duration, documentationVariantId: 'lean_native.trade.average_winning_trade_duration.v1', cssClass: '', source: 'TS.cs 109' },
      { label: 'Avg Loss Duration', value: t.average_losing_trade_duration, documentationVariantId: 'lean_native.trade.average_losing_trade_duration.v1', cssClass: '', source: 'TS.cs 114' },
    ];
  }

  get distributionMetrics(): StatItem[] {
    const t = this.stats().trade;
    const p = this.stats().portfolio;
    return [
      { label: 'P&L Std Dev', value: this.dollar(t.profit_loss_standard_deviation), documentationVariantId: 'lean_native.trade.profit_loss_standard_deviation.v1', cssClass: '', source: 'TS.cs 212' },
      { label: 'Downside Dev', value: this.dollar(t.profit_loss_downside_deviation), documentationVariantId: 'lean_native.trade.profit_loss_downside_deviation.v1', cssClass: '', source: 'TS.cs 219' },
      { label: 'Trade Sharpe', value: this.ratio(t.sharpe_ratio), documentationVariantId: 'lean_native.trade.sharpe_ratio.v1', cssClass: this.valClass(t.sharpe_ratio), source: 'TS.cs 233' },
      { label: 'Trade Sortino', value: this.ratio(t.sortino_ratio), documentationVariantId: 'lean_native.trade.sortino_ratio.v1', cssClass: this.valClass(t.sortino_ratio), source: 'TS.cs 239' },
      { label: 'Avg Win Rate', value: this.pct(p.average_win_rate), documentationVariantId: 'lean_native.portfolio.average_win_rate.v1', cssClass: 'positive', source: 'PS.cs 36' },
      { label: 'Avg Loss Rate', value: this.pct(p.average_loss_rate), documentationVariantId: 'lean_native.portfolio.average_loss_rate.v1', cssClass: 'negative', source: 'PS.cs 42' },
      { label: 'LEAN Expectancy', value: this.ratio(p.expectancy), documentationVariantId: 'lean_native.portfolio.expectancy.v1', cssClass: this.valClass(p.expectancy), source: 'PS.cs 69' },
      { label: 'Profit/Loss Ratio', value: this.ratio(p.profit_loss_ratio), documentationVariantId: 'lean_native.portfolio.profit_loss_ratio.v1', cssClass: this.valClass(p.profit_loss_ratio - 1), source: 'PS.cs 48' },
      { label: 'Win Rate', value: this.pct(p.win_rate), documentationVariantId: 'lean_native.portfolio.win_rate.v1', cssClass: this.valClass(p.win_rate - 0.5), source: 'PS.cs 54' },
      { label: 'Loss Rate', value: this.pct(p.loss_rate), documentationVariantId: 'lean_native.portfolio.loss_rate.v1', cssClass: this.valClass(0.5 - p.loss_rate), source: 'PS.cs 60' },
      { label: 'Portfolio Turnover', value: this.pct(p.portfolio_turnover), documentationVariantId: 'lean_native.portfolio.portfolio_turnover.v1', cssClass: '', source: 'PS.cs 198' },
    ];
  }

  get tradePopulationMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'First Entry', value: this.date(t.start_date_time), documentationVariantId: 'lean_native.trade.start_date_time.v1', source: 'TS.cs 33' },
      { label: 'Last Exit', value: this.date(t.end_date_time), documentationVariantId: 'lean_native.trade.end_date_time.v1', source: 'TS.cs 39' },
      { label: 'Closed Trades', value: String(t.total_number_of_trades), documentationVariantId: 'lean_native.trade.total_number_of_trades.v1', source: 'TS.cs 45' },
      { label: 'Winning Trades', value: String(t.number_of_winning_trades), documentationVariantId: 'lean_native.trade.number_of_winning_trades.v1', cssClass: 'positive', source: 'TS.cs 51' },
      { label: 'Losing Trades', value: String(t.number_of_losing_trades), documentationVariantId: 'lean_native.trade.number_of_losing_trades.v1', cssClass: 'negative', source: 'TS.cs 57' },
      { label: 'Trade Win Rate', value: this.pct(t.win_rate), documentationVariantId: 'lean_native.trade.win_rate.v1', cssClass: this.valClass(t.win_rate - 0.5), source: 'TS.cs 164' },
      { label: 'Trade Loss Rate', value: this.pct(t.loss_rate), documentationVariantId: 'lean_native.trade.loss_rate.v1', cssClass: this.valClass(0.5 - t.loss_rate), source: 'TS.cs 170' },
      { label: 'Win/Loss Count Ratio', value: this.ratio(t.win_loss_ratio), documentationVariantId: 'lean_native.trade.win_loss_ratio.v1', cssClass: this.valClass(t.win_loss_ratio - 1), source: 'TS.cs 158' },
      { label: 'Profit/Loss Ratio', value: this.ratio(t.profit_loss_ratio), documentationVariantId: 'lean_native.trade.profit_loss_ratio.v1', cssClass: this.valClass(t.profit_loss_ratio - 1), source: 'TS.cs 152' },
    ];
  }

  get tradeExcursionMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      { label: 'Average MAE', value: this.dollar(t.average_mae), documentationVariantId: 'lean_native.trade.average_mae.v1', cssClass: 'negative', source: 'TS.cs 176' },
      { label: 'Average MFE', value: this.dollar(t.average_mfe), documentationVariantId: 'lean_native.trade.average_mfe.v1', cssClass: 'positive', source: 'TS.cs 182' },
      { label: 'Largest MAE', value: this.dollar(t.largest_mae), documentationVariantId: 'lean_native.trade.largest_mae.v1', cssClass: 'negative', source: 'TS.cs 188' },
      { label: 'Largest MFE', value: this.dollar(t.largest_mfe), documentationVariantId: 'lean_native.trade.largest_mfe.v1', cssClass: 'positive', source: 'TS.cs 194' },
      { label: 'Max Closed Drawdown', value: this.dollar(t.maximum_closed_trade_drawdown), documentationVariantId: 'lean_native.trade.maximum_closed_trade_drawdown.v1', cssClass: 'negative', source: 'TS.cs 200' },
      { label: 'Max Intra-trade Drawdown', value: this.dollar(t.maximum_intra_trade_drawdown), documentationVariantId: 'lean_native.trade.maximum_intra_trade_drawdown.v1', cssClass: 'negative', source: 'TS.cs 206' },
      { label: 'Max End Drawdown', value: this.dollar(t.maximum_end_trade_drawdown), documentationVariantId: 'lean_native.trade.maximum_end_trade_drawdown.v1', cssClass: 'negative', source: 'TS.cs 253' },
      { label: 'Average End Drawdown', value: this.dollar(t.average_end_trade_drawdown), documentationVariantId: 'lean_native.trade.average_end_trade_drawdown.v1', cssClass: 'negative', source: 'TS.cs 259' },
    ];
  }

  get durationMetrics(): StatItem[] {
    const t = this.stats().trade;
    return [
      ...this.streakMetrics,
      { label: 'Median Duration', value: t.median_trade_duration, documentationVariantId: 'lean_native.trade.median_trade_duration.v1', source: 'TS.cs 119' },
      { label: 'Median Win Duration', value: t.median_winning_trade_duration, documentationVariantId: 'lean_native.trade.median_winning_trade_duration.v1', source: 'TS.cs 124' },
      { label: 'Median Loss Duration', value: t.median_losing_trade_duration, documentationVariantId: 'lean_native.trade.median_losing_trade_duration.v1', source: 'TS.cs 129' },
      { label: 'Max Drawdown Duration', value: t.maximum_drawdown_duration, documentationVariantId: 'lean_native.trade.maximum_drawdown_duration.v1', cssClass: 'negative', source: 'TS.cs 265' },
    ];
  }

  get runtimeMetrics(): StatItem[] {
    const r = this.stats().runtime;
    return [
      { label: 'Equity', value: this.dollar(r.equity), documentationVariantId: 'lean_runtime.equity.v1', cssClass: this.valClass(r.net_profit), source: 'Result.RuntimeStatistics' },
      { label: 'Holdings', value: this.dollar(r.holdings), documentationVariantId: 'lean_runtime.holdings.v1', source: 'Result.RuntimeStatistics' },
      { label: 'Net Profit', value: this.dollar(r.net_profit), documentationVariantId: 'lean_runtime.net_profit.v1', cssClass: this.valClass(r.net_profit), source: 'Result.RuntimeStatistics' },
      { label: 'Total Return', value: this.pct(r.total_return), documentationVariantId: 'lean_runtime.return.v1', cssClass: this.valClass(r.total_return), source: 'Result.RuntimeStatistics' },
      { label: 'Unrealized', value: this.dollar(r.unrealized), documentationVariantId: 'lean_runtime.unrealized.v1', cssClass: this.valClass(r.unrealized), source: 'Result.RuntimeStatistics' },
      { label: 'Fees', value: this.dollar(r.fees), documentationVariantId: 'lean_runtime.fees.v1', cssClass: this.valClass(r.fees), source: 'Result.RuntimeStatistics' },
      { label: 'Volume', value: this.dollar(r.volume), documentationVariantId: 'lean_runtime.volume.v1', source: 'Result.RuntimeStatistics' },
      { label: 'Orders', value: String(r.total_orders), documentationVariantId: 'lean_runtime.total_orders.v1', source: 'Result.RuntimeStatistics' },
      { label: 'Runtime PSR', value: this.pct(r.probabilistic_sharpe_ratio), documentationVariantId: 'lean_runtime.probabilistic_sharpe_ratio.v1', cssClass: this.valClass(r.probabilistic_sharpe_ratio - 0.5), source: 'Result.RuntimeStatistics' },
      { label: 'Trade Fees', value: this.dollar(this.stats().trade.total_fees), documentationVariantId: 'lean_native.trade.total_fees.v1', cssClass: 'negative', source: 'TS.cs 271' },
    ];
  }
}
