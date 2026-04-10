import {
  Component, input, signal, computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  StrategyLabChartComponent,
  ChartBar,
  ChartIndicatorResult,
} from '../strategy-lab-chart/strategy-lab-chart.component';
import { LeanStatisticsComponent } from '../lean-statistics/lean-statistics.component';
import type { LeanStatistics } from '../strategy-lab.component';

export interface BacktestTradeDisplay {
  trade_number: number;
  trade_type: string;
  entry_timestamp: string;
  exit_timestamp: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  cumulative_pnl_pct: number;
  signal_reason: string;
  indicator_snapshot: Record<string, number | null>;
}

export interface BacktestResultData {
  success: boolean;
  ticker: string;
  strategy_name: string;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  win_loss_ratio: number;
  profit_factor: number;
  expectancy_per_trade: number;
  total_pnl_pct: number;
  total_pnl_pts: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  lean_statistics: import('../strategy-lab.component').LeanStatistics | null;
  source_bars: number;
  rth_bars: number;
  resampled_bars: number;
  bars_processed: number;
  timeframe: string;
  chart_bars: ChartBar[];
  chart_indicators: ChartIndicatorResult[];
  quality: any;
  trades: BacktestTradeDisplay[];
}

@Component({
  selector: 'app-backtest-results',
  standalone: true,
  imports: [CommonModule, StrategyLabChartComponent, LeanStatisticsComponent],
  templateUrl: './backtest-results.component.html',
  styleUrls: ['./backtest-results.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BacktestResultsComponent {
  result = input.required<BacktestResultData>();
  session = input<string>('rth');
  isParityValidated = input(false);
  parityReference = input('');
  fromDate = input('');
  toDate = input('');

  showTradeLog = signal(false);

  leanStats = computed(() => this.result().lean_statistics ?? null);
  chartBars = computed(() => this.result().chart_bars ?? []);
  chartIndicators = computed(() => this.result().chart_indicators ?? []);
  chartQuality = computed(() => this.result().quality ?? null);
  chartTrades = computed(() => this.result().trades ?? []);

  formatPct(val: number): string {
    return (val * 100).toFixed(3) + '%';
  }
}
