import { ChangeDetectionStrategy, Component, computed, effect, input, signal, untracked } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay, TimestampDisplayComponent } from '../../shared/timestamp';
import { OptionsMonthGridComponent, type MonthCell, type MonthMetric } from '../options-backtest/run/options-month-grid.component';
import { OptionsEvidenceChartComponent } from '../options-backtest/run/options-evidence-chart.component';
import { optionsSearchErrorMessage } from './options-golden-search.service';
import type { EvidenceWindow, SearchCandidate, SessionLoader, SessionResult } from './options-golden-search.types';

type SessionRead = { readonly kind: 'loading' } | { readonly kind: 'ready'; readonly value: SessionResult } | { readonly kind: 'error'; readonly message: string };

/** Exact values remain in labels and tables; shared chart geometry only positions the lines. */
@Component({
  selector: 'app-options-search-candidate',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent, OptionsEvidenceChartComponent, OptionsMonthGridComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-candidate.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsSearchCandidateComponent {
  readonly candidate = input.required<SearchCandidate>();
  readonly studyId = input('');
  readonly window = input<EvidenceWindow>('development');
  readonly sessionLoader = input<SessionLoader | null>(null);
  protected readonly sessionReads = signal<ReadonlyMap<number, SessionRead>>(new Map());
  private readonly identity = computed(() => `${this.studyId()}:${this.candidate().candidate_hash}:${this.window()}`);

  constructor() {
    effect(() => {
      this.identity();
      untracked(() => {
        this.sessionReads.set(new Map());
        this.openSessions.set(new Set());
        this.sessionsOpen.set(false);

      });
    });
  }
  protected readonly sessionsOpen = signal(false);
  protected readonly openSessions = signal<ReadonlySet<number>>(new Set());

  protected toggleSessions(event: Event): void {
    if (event.target instanceof HTMLDetailsElement && event.target.parentElement?.classList.contains('candidate')) this.sessionsOpen.set(event.target.open);
  }

  protected toggleSession(session: SessionResult, event: Event): void {
    if (!(event.target instanceof HTMLDetailsElement)) return;
    const next = new Set(this.openSessions());
    if (event.target.open) { next.add(session.session_open_ms); void this.loadSession(session); } else next.delete(session.session_open_ms);
    this.openSessions.set(next);
  }
  protected async loadSession(session: SessionResult): Promise<void> {
    const loader = this.sessionLoader();
    const existing = this.sessionReads().get(session.session_open_ms);
    if (loader === null || session.attempts.length > 0 || existing?.kind === 'loading' || existing?.kind === 'ready') return;
    const identity = this.identity();
    this.putRead(session.session_open_ms, { kind: 'loading' });
    try {
      const value = await loader(this.studyId(), this.candidate().candidate_hash, this.window(), session.session_open_ms);
      if (identity !== this.identity()) return;
      if (value.session_open_ms !== session.session_open_ms) throw new Error('The returned evidence belongs to another session.');
      this.putRead(session.session_open_ms, { kind: 'ready', value });
    } catch (error) {
      if (identity === this.identity()) this.putRead(session.session_open_ms, { kind: 'error', message: optionsSearchErrorMessage(error, 'Leg attempts could not be loaded. Retry to read this exact session.') });
    }
  }

  protected sessionDetail(summary: SessionResult): SessionResult {
    const read = this.sessionReads().get(summary.session_open_ms);
    return read?.kind === 'ready' ? read.value : summary;
  }

  private putRead(session: number, read: SessionRead): void {
    this.sessionReads.update((reads) => new Map([...reads, [session, read]]));
  }

  protected readonly equity = computed(() => this.candidate().equity.map((point) => ({ timestamp_ms: point.timestamp_ms, value: point.equity_usd })));
  protected readonly drawdown = computed(() => this.candidate().equity.map((point) => ({ timestamp_ms: point.timestamp_ms, value: point.drawdown_usd })));
  protected readonly monthMetrics: readonly MonthMetric[] = [
    { key: 'pnl', label: 'Net P&L', unit: 'usd', signed: true },
    { key: 'sessions', label: 'Traded sessions', unit: 'count', signed: false },
  ];
  protected readonly monthCells = computed<readonly MonthCell[]>(() => this.candidate().monthly.map((row) => {
    const [year, month] = formatTimestampDisplay(row.month_ms, { mode: 'date-et' }).split('-');
    return { year: Number(year), month: Number(month), values: { pnl: row.net_profit_usd, sessions: String(row.completed_sessions) } };
  }));
}
