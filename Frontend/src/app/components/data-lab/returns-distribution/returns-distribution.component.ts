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

import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { etIsoDate } from '../../../shared/date/et-midnight';
import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import {
  CaptureStatus,
  HistogramBin,
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
  startMsUtc: number | null;
  endMsUtc: number | null;
  binWidthPct: number;
}

/** Exhaustive capture-status copy — one branch per member of the closed
 * Python contract, so a new state fails to compile here rather than
 * rendering as a stale "already up to date". */
function captureLineFor(receipt: {
  status: CaptureStatus;
  fetchedArtifactCount: number;
  detail: string | null;
}): string | null {
  switch (receipt.status) {
    case 'not_attempted':
      return null;
    case 'skipped':
      return null;
    case 'complete':
      return receipt.fetchedArtifactCount > 0
        ? `This request populated the data lake with ${receipt.fetchedArtifactCount} artifact(s) first.`
        : 'This request found the data lake already up to date.';
    case 'partial':
      return (
        `This request captured ${receipt.fetchedArtifactCount} artifact(s), but the lake is still missing some ` +
        `sessions — the study covers what was captured${receipt.detail ? ` (${receipt.detail})` : ''}.`
      );
    case 'failed':
      return (
        'This request could not populate the data lake' +
        `${receipt.detail ? ` (${receipt.detail})` : ''} — it shows whatever the lake already held.`
      );
  }
}

/**
 * The Return Distribution study: a histogram of daily moves over the Data
 * Lab's committed ticker + window, populated entirely by Python (bins,
 * normal overlay, statistics, per-day session segments, bin membership).
 * Toggling the return kind or clicking a basket is presentation-only;
 * changing scope or bin width refetches. The window travels to Python as
 * the workspace's int64 ms UTC numbers — no client-side date math.
 */
@Component({
  selector: 'app-returns-distribution',
  templateUrl: './returns-distribution.component.html',
  styleUrl: './returns-distribution.component.scss',
  imports: [DecimalPipe, AssetIdentityComponent, ReturnsHistogramChartComponent, BinDrillDownComponent, DayCandlesComponent],
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

  /** The study window is the shared workspace scope, passed through
   * verbatim (half-open [start, end) ms UTC); Python resolves the
   * inclusive calendar dates. */
  readonly windowStartMs = computed(() => this.store.committedWindow()?.startMsUtc ?? null);
  readonly windowEndMs = computed(() => this.store.committedWindow()?.endMsUtc ?? null);

  readonly study = rxResource<ReturnDistributionStudy, StudyRequest>({
    params: (): StudyRequest => ({
      ticker: this.ticker(),
      startMsUtc: this.windowStartMs(),
      endMsUtc: this.windowEndMs(),
      binWidthPct: this.binWidthPct(),
    }),
    stream: ({ params }) =>
      params.ticker && params.startMsUtc !== null && params.endMsUtc !== null
        ? this.service.distribution({
            symbol: params.ticker,
            fromMsUtc: params.startMsUtc,
            toMsUtc: params.endMsUtc,
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

  /** One-line receipt of the on-demand lake capture, when one ran. */
  readonly captureLine = computed<string | null>(() => {
    const capture = this.study.value()?.capture;
    return capture ? captureLineFor(capture) : null;
  });

  readonly selectedBinInfo = computed<{ bin: HistogramBin; index: number } | null>(() => {
    const dist = this.activeKindDistribution();
    const index = this.selectedBinIndex();
    if (!dist || index === null) return null;
    const bin = dist.bins[index];
    return bin ? { bin, index } : null;
  });

  private static fmt(value: number | null, digits: number, suffix = ''): string {
    return value === null ? '—' : `${value.toFixed(digits)}${suffix}`;
  }

  readonly statsRows = computed(() => {    const dist = this.activeKindDistribution();
    if (!dist) return [];
    const s = dist.stats;
    return [
      { label: 'Days studied', value: String(s.nDays), hint: 'Sessions with usable data in the window' },
      { label: 'Average daily move', value: `${s.meanPct.toFixed(3)}%`, hint: 'Mean of the daily returns' },
      { label: 'Daily volatility (σ)', value: ReturnsDistributionComponent.fmt(s.stdPct, 3, '%'), hint: 'Sample standard deviation (ddof=1)' },
      { label: 'Annualized vol', value: ReturnsDistributionComponent.fmt(s.annualizedVolPct, 1, '%'), hint: 'σ × √252' },
      { label: 'Skewness', value: ReturnsDistributionComponent.fmt(s.skewness, 2), hint: 'Sign of the tail: negative = crash-prone (undefined for flat series)' },
      { label: 'Excess kurtosis', value: ReturnsDistributionComponent.fmt(s.excessKurtosis, 2), hint: 'Fat tails: 0 = normal, higher = more wild days (undefined for flat series)' },
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
        ? 'Could not capture this symbol into the data lake'
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
    if (Number.isFinite(value) && value > 0 && value !== this.binWidthPct()) {
      this.binWidthPct.set(value);
      // The retained index refers to a different interval under the new
      // geometry: drop the basket and day selection rather than showing
      // unrelated days or stale candles.
      this.selectedBinIndex.set(null);
      this.selectedDayMs.set(null);
    }
  }
}
