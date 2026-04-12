import {
  Component, input, signal, computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { TooltipModule } from 'primeng/tooltip';
import { LeanStatisticsComponent } from '../../strategy-lab/lean-statistics/lean-statistics.component';

// ── Shared types (mirrored from lean-engine) ──────────────────
export interface LeanPortfolioStats {
  average_win_rate: number; average_loss_rate: number; profit_loss_ratio: number;
  win_rate: number; loss_rate: number; expectancy: number;
  start_equity: number; end_equity: number; total_net_profit: number;
  compounding_annual_return: number; sharpe_ratio: number; sortino_ratio: number;
  probabilistic_sharpe_ratio: number; annual_standard_deviation: number;
  annual_variance: number; alpha: number; beta: number;
  information_ratio: number; tracking_error: number; treynor_ratio: number;
  drawdown: number; drawdown_recovery: number;
  value_at_risk_99: number; value_at_risk_95: number; portfolio_turnover: number;
}

export interface LeanTradeStats {
  start_date_time: string; end_date_time: string;
  total_number_of_trades: number; number_of_winning_trades: number;
  number_of_losing_trades: number; total_profit_loss: number;
  total_profit: number; total_loss: number;
  largest_profit: number; largest_loss: number;
  average_profit_loss: number; average_profit: number; average_loss: number;
  average_trade_duration: string; average_winning_trade_duration: string;
  average_losing_trade_duration: string;
  max_consecutive_winning_trades: number; max_consecutive_losing_trades: number;
  profit_factor: number; profit_to_max_drawdown_ratio: number;
  profit_loss_standard_deviation: number; profit_loss_downside_deviation: number;
  sharpe_ratio: number; sortino_ratio: number; total_fees: number;
}

export interface LeanRuntimeStats {
  equity: number; fees: number; net_profit: number;
  total_return: number; total_orders: number;
}

export interface LeanStatistics {
  portfolio: LeanPortfolioStats;
  trade: LeanTradeStats;
  runtime: LeanRuntimeStats;
}

export interface EngineTrade {
  trade_number: number;
  entry_time: string;
  entry_price: number;
  exit_time: string;
  exit_price: number;
  indicators: Record<string, number>;
  pnl_pts: number;
  pnl_pct: number;
  result: string;
  signal_reason: string;
}

export interface EngineResultData {
  success: boolean;
  strategy_name: string;
  fill_mode: string;
  initial_cash: number;
  final_equity: number;
  net_profit: number;
  total_fees: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  statistics: Record<string, number | null>;
  lean_statistics: LeanStatistics | null;
  trades: EngineTrade[];
  log_lines: string[];
  error?: string;
}

@Component({
  selector: 'app-engine-results',
  standalone: true,
  imports: [CommonModule, FormsModule, TooltipModule, LeanStatisticsComponent],
  templateUrl: './engine-results.component.html',
  styleUrls: ['./engine-results.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EngineResultsComponent {
  result = input.required<EngineResultData>();
  symbol = input<string>('SPY');

  showTradeLog = signal(false);
  selectedTimezone = signal<string>('UTC');

  leanStats = computed(() => this.result().lean_statistics ?? null);

  totalFees = computed(() => this.result().total_fees ?? 0);

  feePerTrade = computed(() => {
    const r = this.result();
    if (r.total_trades === 0) return 0;
    return r.total_fees / r.total_trades;
  });

  feeDragPct = computed(() => {
    const r = this.result();
    const grossProfit = r.net_profit + r.total_fees;
    if (grossProfit <= 0) return 0;
    return r.total_fees / grossProfit;
  });

  get timezoneOptions(): { value: string; label: string }[] {
    const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const base = [
      { value: 'UTC', label: 'UTC' },
      { value: 'America/New_York', label: 'New York (ET)' },
      { value: 'America/Los_Angeles', label: 'Los Angeles (PT)' },
      { value: 'Europe/London', label: 'London' },
      { value: 'Asia/Kolkata', label: 'Mumbai (IST)' },
      { value: 'Asia/Tokyo', label: 'Tokyo (JST)' },
    ];
    return base.some(o => o.value === localZone)
      ? base
      : [...base, { value: localZone, label: `Local (${localZone})` }];
  }

  formatCurrency(value: number | null | undefined): string {
    if (value == null || Number.isNaN(value)) return '—';
    return new Intl.NumberFormat('en-US', {
      style: 'currency', currency: 'USD', maximumFractionDigits: 2,
    }).format(value);
  }

  formatPct(val: number): string {
    return (val * 100).toFixed(2) + '%';
  }

  formatNumber(value: number | null | undefined, places = 2): string {
    if (value == null || Number.isNaN(value)) return '—';
    return value.toFixed(places);
  }

  formatTradeTime(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const zone = this.selectedTimezone();
    if (zone === 'UTC') return d.toISOString().replace(/\.\d{3}Z$/, 'Z');

    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: zone, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).formatToParts(d).reduce<Record<string, string>>((acc, p) => {
      if (p.type !== 'literal') acc[p.type] = p.value;
      return acc;
    }, {});

    const hh = parts['hour'] === '24' ? '00' : parts['hour'];
    return `${parts['year']}-${parts['month']}-${parts['day']}T${hh}:${parts['minute']}:${parts['second']}`;
  }

  tradeIndicatorEntries(trade: EngineTrade): { key: string; value: number }[] {
    return Object.entries(trade.indicators).map(([key, value]) => ({ key, value }));
  }

  isOptionsIndicators(trade: EngineTrade): boolean {
    return 'spread_type' in trade.indicators;
  }

  private static readonly SIGNAL_KEYS = new Set(['ema5', 'ema10', 'rsi']);
  private static readonly SPREAD_KEYS = new Set([
    'spread_type', 'expiration_dte', 'spread_width',
    'underlying_entry', 'underlying_exit', 'pricing_mode',
  ]);
  private static readonly LONG_LEG_KEYS = new Set([
    'long_strike', 'long_entry', 'long_exit', 'long_delta',
  ]);
  private static readonly SHORT_LEG_KEYS = new Set([
    'short_strike', 'short_entry', 'short_exit', 'short_delta',
  ]);
  private static readonly PNL_KEYS = new Set([
    'dollar_pnl', 'max_profit', 'max_loss',
  ]);

  groupedIndicators(trade: EngineTrade): {
    signal: { key: string; value: number }[];
    spread: { key: string; value: number }[];
    longLeg: { key: string; value: number }[];
    shortLeg: { key: string; value: number }[];
    pnl: { key: string; value: number }[];
  } {
    const signal: { key: string; value: number }[] = [];
    const spread: { key: string; value: number }[] = [];
    const longLeg: { key: string; value: number }[] = [];
    const shortLeg: { key: string; value: number }[] = [];
    const pnl: { key: string; value: number }[] = [];

    for (const [key, value] of Object.entries(trade.indicators)) {
      const entry = { key, value };
      if (EngineResultsComponent.SIGNAL_KEYS.has(key)) signal.push(entry);
      else if (EngineResultsComponent.SPREAD_KEYS.has(key)) spread.push(entry);
      else if (EngineResultsComponent.LONG_LEG_KEYS.has(key)) longLeg.push(entry);
      else if (EngineResultsComponent.SHORT_LEG_KEYS.has(key)) shortLeg.push(entry);
      else if (EngineResultsComponent.PNL_KEYS.has(key)) pnl.push(entry);
      else spread.push(entry); // fallback: anything unknown goes to spread group
    }

    return { signal, spread, longLeg, shortLeg, pnl };
  }

  downloadTradesCsv(): void {
    const r = this.result();
    const header = '#,Entry Time,Entry Price,Exit Time,Exit Price,PnL (pts),PnL %,Result,Signal,Indicators';
    const rows = r.trades.map(t => {
      const indicators = Object.entries(t.indicators)
        .map(([k, v]) => `${k}=${v.toFixed(4)}`).join('; ');
      return [
        t.trade_number,
        this.formatTradeTime(t.entry_time),
        t.entry_price.toFixed(2),
        this.formatTradeTime(t.exit_time),
        t.exit_price.toFixed(2),
        t.pnl_pts.toFixed(4),
        (t.pnl_pct * 100).toFixed(4) + '%',
        t.result,
        `"${t.signal_reason}"`,
        `"${indicators}"`,
      ].join(',');
    });
    const csv = [header, ...rows].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${this.symbol()}_${r.strategy_name}_trades.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
}
