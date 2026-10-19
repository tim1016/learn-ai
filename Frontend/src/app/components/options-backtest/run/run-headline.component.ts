import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { RunFigures } from '../options-backtest.types';
import { isLoss, scaledBarPercents } from './run-chart-scale';

interface Metric {
  readonly key: string;
  readonly label: string;
  readonly value: string | null;
  readonly unit: 'usd' | 'ratio' | 'percent' | 'text';
  readonly context: string;
  readonly definition: string;
  readonly missing?: string;
  readonly bar?: string;
}

/** One statistical desk; Python authors every quantity, including means and ratios. */
@Component({
  selector: 'app-options-backtest-run-headline',
  imports: [AuthoredUsdPipe, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-headline.component.html',
  styleUrl: './run-headline.component.scss',
})
export class RunHeadlineComponent {
  readonly figures = input.required<RunFigures>();
  protected readonly inspected = signal<string | null>(null);
  protected readonly loss = computed(() => isLoss(this.figures().net_profit_usd));
  protected readonly summary = computed<readonly Metric[]>(() => {
    const f = this.figures(), d = f.dashboard, fall = f.drawdown_by_dollars;
    return [
      { key: 'profit', label: 'Net profit', value: f.net_profit_usd, unit: 'usd', context: 'After modeled costs', definition: 'Net results of the recorded leg round trips, including marked legs, after fees and modeled slippage.' },
      { key: 'sharpe', label: 'Annualized Sharpe', value: d?.annualized_sharpe == null ? null : d.display_annualized_sharpe ?? d.annualized_sharpe, unit: 'ratio', context: 'Daily returns · 0% risk-free', missing: d ? 'Unavailable' : 'Not recorded', definition: 'Daily net P&L divided by fixed starting capital, matching Options Golden Search. Mean excess daily return divided by sample standard deviation, multiplied by the square root of calendar-derived sessions per year. Genuine no-trade sessions enter as zeros; missing evidence never does.' },
      { key: 'drawdown', label: 'Max drawdown', value: fall?.fall_usd ?? null, unit: 'usd', context: fall ? `${fall.display_percent_1_place}% of peak · session closes` : 'Session-close equity', missing: 'No drawdown', definition: 'The largest dollar fall below a previous session-close equity peak. Session closes can hide deeper intraday losses. The shaded chart span names this peak and trough.' },
      { key: 'recovery', label: 'Drawdown recovery', value: !d ? null : d.risk_unavailable_reason ? 'Unavailable' : !fall ? 'No drawdown' : d.recovery_sessions === null || d.recovery_sessions === undefined ? 'Not recovered' : `${d.recovery_sessions} sessions`, unit: 'text', context: 'From trough to previous peak', definition: 'Sessions after the largest dollar-drawdown trough until the first close at or above its recorded peak. No-trade sessions count. This differs from the longest underwater stretch across all drawdowns.' },
      { key: 'factor', label: 'Session profit factor', value: d?.session_profit_factor ?? null, unit: 'text', context: 'Whole-setup session P&L', missing: !d ? 'Not recorded' : f.sessions_traded === 0 ? 'No traded sessions' : 'No losing session', definition: 'Positive net session results divided by absolute negative net session results, all legs and re-entries combined. Leg-level profit factor is kept separately below.' },
    ];
  });
  protected readonly economics = computed<readonly Metric[]>(() => {
    const f = this.figures(), d = f.dashboard;
    const bars = scaledBarPercents([d?.average_winning_session_usd ?? '0', d?.average_losing_session_usd ?? '0'], 100);
    return [
      { key: 'win', label: 'Average winning session', value: d?.average_winning_session_usd ?? null, unit: 'usd', context: `${f.sessions_profitable} winning sessions`, bar: bars[0], missing: d ? 'No winning session' : 'Not recorded', definition: 'Mean net P&L of positive traded sessions, all legs combined after costs. Displayed to the cent, half-even. Break-even and no-trade sessions are outside this denominator.' },
      { key: 'loss', label: 'Average losing session', value: d?.average_losing_session_usd ?? null, unit: 'usd', context: `${f.sessions_losing} losing sessions`, bar: bars[1], missing: d ? 'No losing session' : 'Not recorded', definition: 'Mean net P&L of negative traded sessions. Losses stay negative; all legs and re-entries are combined after costs. Displayed to the cent, half-even.' },
      { key: 'mean', label: 'Average traded session', value: d?.average_session_usd ?? null, unit: 'usd', context: `${f.sessions_traded} traded sessions`, missing: d ? 'No traded sessions' : 'Not recorded', definition: 'Mean net P&L across winning, losing and break-even traded sessions. No-trade sessions are excluded here and included in daily Sharpe. This descriptive mean is not a forecast.' },
      { key: 'rate', label: 'Session win rate', value: f.daily_win_rate?.display_percent_1_place ?? null, unit: 'percent', context: `${f.sessions_profitable} won · ${f.sessions_losing} lost · ${f.sessions_no_trade} no trade`, missing: 'No traded sessions', definition: 'Profitable sessions divided by traded sessions. This counts the complete setup per session; trade win rate below counts individual leg round trips.' },
    ];
  });
  protected readonly detail = computed(() => [...this.summary(), ...this.economics()].find((metric) => metric.key === this.inspected()) ?? null);
  protected inspect(key: string): void { this.inspected.update((current) => current === key ? null : key); }
}
