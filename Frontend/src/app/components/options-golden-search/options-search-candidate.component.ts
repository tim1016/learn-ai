import { ChangeDetectionStrategy, Component, computed, effect, input, signal, untracked } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay, TimestampDisplayComponent } from '../../shared/timestamp';
import { equityCoordinates } from '../options-backtest/run/run-chart-scale';
import { optionsSearchErrorMessage } from './options-golden-search.service';
import type { EvidenceWindow, SearchCandidate, SessionLoader, SessionResult } from './options-golden-search.types';

type SessionRead = { readonly kind: 'loading' } | { readonly kind: 'ready'; readonly value: SessionResult } | { readonly kind: 'error'; readonly message: string };

/** Exact values remain in labels and tables; shared chart geometry only positions the lines. */
@Component({
  selector: 'app-options-search-candidate',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
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
        this.chartOpen.set(false);
      });
    });
  }
  protected readonly chartOpen = signal(false);
  protected readonly sessionsOpen = signal(false);
  protected readonly openSessions = signal<ReadonlySet<number>>(new Set());

  protected toggleChart(event: Event): void {
    if (event.target instanceof HTMLDetailsElement) this.chartOpen.set(event.target.open);
  }

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

  protected readonly lines = computed(() => {
    const points = this.candidate().equity;
    const equity = equityCoordinates(points.map((point) => point.equity_usd));
    const drawdown = equityCoordinates(points.map((point) => point.drawdown_usd));
    return [
      { title: 'Minute-close equity (USD)', line: equity.map((point) => `${point.x},${point.y}`).join(' '), points: points.map((point, index) => ({ ...equity[index], at: point.timestamp_ms, title: `${formatTimestampDisplay(point.timestamp_ms, { mode: 'et' })}: ${point.equity_usd} USD` })) },
      { title: 'Minute-close drawdown (USD)', line: drawdown.map((point) => `${point.x},${point.y}`).join(' '), points: points.map((point, index) => ({ ...drawdown[index], at: point.timestamp_ms, title: `${formatTimestampDisplay(point.timestamp_ms, { mode: 'et' })}: ${point.drawdown_usd} USD` })) },
    ];
  });
}
