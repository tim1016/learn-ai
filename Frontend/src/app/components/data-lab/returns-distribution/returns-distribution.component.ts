import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { EMPTY } from 'rxjs';

import { etIsoDate } from '../../../shared/date/et-midnight';
import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import {
  ReturnDistributionStudy,
  ReturnKind,
  ReturnsDistributionService,
} from './returns-distribution.service';
import { ReturnsHistogramChartComponent } from './returns-histogram-chart/returns-histogram-chart.component';
import { BinDrillDownComponent } from './bin-drill-down/bin-drill-down.component';
import { DayCandlesComponent } from './day-candles/day-candles.component';

const KIND_LABELS: readonly { kind: ReturnKind; label: string; hint: string }[] = [
  { kind: 'close_to_close', label: 'Close → close', hint: 'The headline daily move, including the overnight gap' },
  { kind: 'session', label: 'Session only', hint: 'Open → close: what happened while the market was open' },
  { kind: 'overnight', label: 'Overnight gap', hint: 'Previous close → today’s open' },
];

interface StudyRequest {
  ticker: string;
  fromDate: string | null;
  toDate: string | null;
  binWidthPct: number;
}

/**
 * The Return Distribution study: a histogram of daily moves over the Data
 * Lab's committed ticker + window, populated entirely by Python (bins,
 * normal overlay, statistics, per-day session segments). Toggling the return
 * kind or clicking a basket is presentation-only; changing scope or bin
 * width refetches.
 */
@Component({
  selector: 'app-returns-distribution',
  templateUrl: './returns-distribution.component.html',
  styleUrl: './returns-distribution.component.scss',
  imports: [DecimalPipe, ReturnsHistogramChartComponent, BinDrillDownComponent, DayCandlesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReturnsDistributionComponent {
  readonly store = inject(DataLabWorkspaceStore);
  private readonly service = inject(ReturnsDistributionService);

  readonly kindLabels = KIND_LABELS;
  readonly kind = signal<ReturnKind>('close_to_close');
  readonly binWidthPct = signal<number>(0.5);
  readonly selectedBinIndex = signal<number | null>(null);
  readonly selectedDayMs = signal<number | null>(null);

  readonly ticker = computed(() => this.store.committedTicker());

  /** The study window comes from the shared workspace scope: the committed
   * window's UTC dates are the calendar window the study requests. */
  readonly fromDate = computed(() => {
    const window = this.store.committedWindow();
    return window ? etIsoDate(window.startMsUtc) : null;
  });
  readonly toDate = computed(() => {
    const window = this.store.committedWindow();
    return window ? etIsoDate(window.endMsUtc) : null;
  });

  readonly study = rxResource<ReturnDistributionStudy, StudyRequest>({
    params: (): StudyRequest => ({
      ticker: this.ticker(),
      fromDate: this.fromDate(),
      toDate: this.toDate(),
      binWidthPct: this.binWidthPct(),
    }),
    stream: ({ params }) =>
      params.ticker && params.fromDate && params.toDate
        ? this.service.distribution({
            symbol: params.ticker,
            fromDate: params.fromDate,
            toDate: params.toDate,
            binWidthPct: params.binWidthPct,
            spanPct: 5,
          })
        : EMPTY,
  });

  readonly activeKindDistribution = computed(() => {
    const kinds = this.study.value()?.kinds ?? [];
    return kinds.find((k) => k.kind === this.kind()) ?? null;
  });

  readonly warnings = computed(() => this.study.value()?.warnings ?? []);

  readonly binWidthValue = computed(() => String(this.binWidthPct()));

  readonly selectedBin = computed(() => {
    const dist = this.activeKindDistribution();
    const index = this.selectedBinIndex();
    if (!dist || index === null) return null;
    return dist.bins[index] ?? null;
  });

  readonly statsRows = computed(() => {    const dist = this.activeKindDistribution();
    if (!dist) return [];
    const s = dist.stats;
    return [
      { label: 'Days studied', value: String(s.nDays), hint: 'Sessions with usable data in the window' },
      { label: 'Average daily move', value: `${s.meanPct.toFixed(3)}%`, hint: 'Mean of the daily returns' },
      { label: 'Daily volatility (σ)', value: `${s.stdPct.toFixed(3)}%`, hint: 'Sample standard deviation (ddof=1)' },
      { label: 'Annualized vol', value: `${s.annualizedVolPct.toFixed(1)}%`, hint: 'σ × √252' },
      { label: 'Skewness', value: s.skewness.toFixed(2), hint: 'Sign of the tail: negative = crash-prone' },
      { label: 'Excess kurtosis', value: s.excessKurtosis.toFixed(2), hint: 'Fat tails: 0 = normal, higher = more wild days' },
      { label: 'VaR 95% (1 day)', value: `${s.var95Pct.toFixed(2)}%`, hint: 'On the worst 5% of days you lose at least this much' },
      { label: 'CVaR 95% (1 day)', value: `${s.cvar95Pct.toFixed(2)}%`, hint: 'Average loss across those worst 5% of days' },
      { label: 'Best day', value: `${s.bestDay.valuePct.toFixed(2)}%`, hint: etIsoDate(s.bestDay.sessionOpenMsUtc) },
      { label: 'Worst day', value: `${s.worstDay.valuePct.toFixed(2)}%`, hint: etIsoDate(s.worstDay.sessionOpenMsUtc) },
    ];
  });

  /** Narrowed error surface for the template: the typed detail the FastAPI
   * router attaches (code, message, captured symbols), or a generic fallback
   * for unexpected transport errors. */
  readonly errorInfo = computed<{
    title: string;
    message: string;
    capturedSymbols: readonly string[];
  } | null>(() => {
    const error: unknown = this.study.error();
    if (error === undefined) return null;
    const detail = (error as { error?: { detail?: Record<string, unknown> } }).error?.detail;
    const code = typeof detail?.['error_code'] === 'string' ? detail['error_code'] : null;
    const message =
      typeof detail?.['message'] === 'string'
        ? detail['message']
        : 'Unexpected error while running the study';
    const captured = detail?.['captured_symbols'];
    const capturedSymbols = Array.isArray(captured)
      ? captured.filter((s): s is string => typeof s === 'string')
      : [];
    const title =
      code === 'NOT_CAPTURED'
        ? 'Symbol not captured in the data lake'
        : code === 'INSUFFICIENT_COVERAGE'
          ? 'Not enough captured history'
          : 'The study could not run';
    return { title, message, capturedSymbols };
  });

  onBinSelected(index: number): void {
    this.selectedBinIndex.update((current) => (current === index ? null : index));
    this.selectedDayMs.set(null);
  }

  onDaySelected(sessionOpenMsUtc: number): void {
    this.selectedDayMs.set(sessionOpenMsUtc);
  }

  onBinWidthChange(event: Event): void {
    const value = Number((event.target as HTMLSelectElement).value);
    if (Number.isFinite(value) && value > 0) this.binWidthPct.set(value);
  }
}
