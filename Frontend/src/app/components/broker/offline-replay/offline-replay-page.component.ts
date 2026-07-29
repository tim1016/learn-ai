import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  afterNextRender,
  computed,
  inject,
  signal,
} from '@angular/core';

import type {
  OfflineReplayCatalogResponse,
  OfflineReplayCatalogSession,
  OfflineReplayCommandAction,
  OfflineReplaySession,
  OfflineReplaySpeed,
} from '../../../api/offline-replay.types';
import { OfflineReplayService } from '../../../services/offline-replay.service';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { formatTimestampDisplay } from '../../../shared/timestamp/timestamp-display';
import { extractServerMessage } from '../operation-error';

const ACTIVE_STATUSES = new Set([
  'preparing',
  'warming_up',
  'running',
  'paused',
  'stopping',
]);

@Component({
  selector: 'app-offline-replay-page',
  imports: [
    CommonModule,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './offline-replay-page.component.html',
  styleUrl: './offline-replay-page.component.scss',
})
export class OfflineReplayPageComponent {
  private readonly replay = inject(OfflineReplayService);
  private readonly destroyRef = inject(DestroyRef);
  private pollHandle: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;

  readonly catalog = signal<OfflineReplayCatalogResponse | null>(null);
  readonly recentSessions = signal<OfflineReplaySession[]>([]);
  readonly currentSession = signal<OfflineReplaySession | null>(null);
  readonly selectedSessionDateMs = signal<number | null>(null);
  readonly playbackMinutes = signal<30 | 60>(60);
  readonly speed = signal<OfflineReplaySpeed>(60);
  readonly loading = signal(true);
  readonly acting = signal(false);
  readonly errorMessage = signal<string | null>(null);
  readonly playbackSpeeds: readonly OfflineReplaySpeed[] = [1, 10, 60];

  readonly dateOptions = computed(() =>
    (this.catalog()?.sessions ?? []).filter((session) => session.eligible).map(
      (session) => ({
        session,
        label: formatTimestampDisplay(session.session_date_ms, { mode: 'date-et' }),
      }),
    ),
  );

  readonly active = computed(() => {
    const status = this.currentSession()?.status;
    return status !== undefined && ACTIVE_STATUSES.has(status);
  });

  readonly visibleBarsProcessed = computed(() => {
    const session = this.currentSession();
    if (!session || session.bots.length === 0) return 0;
    const warmupBars = Math.round(
      (session.warmup_end_ms - session.session_open_ms) / 60_000,
    );
    return Math.max(
      0,
      Math.min(...session.bots.map((bot) => bot.bars_processed)) - warmupBars,
    );
  });

  readonly progressPercent = computed(() => {
    const session = this.currentSession();
    if (!session) return 0;
    return Math.min(100, (this.visibleBarsProcessed() / session.playback_minutes) * 100);
  });

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.destroyed = true;
      if (this.pollHandle !== null) clearTimeout(this.pollHandle);
    });
    afterNextRender(() => {
      setTimeout(() => void this.initialize(), 0);
    });
  }

  async initialize(): Promise<void> {
    this.loading.set(true);
    this.errorMessage.set(null);
    try {
      const [catalog, sessions] = await Promise.all([
        this.replay.getCatalog(),
        this.replay.listSessions(),
      ]);
      this.catalog.set(catalog);
      this.recentSessions.set(sessions);
      this.selectedSessionDateMs.set(
        catalog.recommended_session_date_ms
          ?? catalog.sessions.find((session) => session.eligible)?.session_date_ms
          ?? null,
      );
      if (sessions.length > 0) {
        this.currentSession.set(
          sessions.find((session) => ACTIVE_STATUSES.has(session.status))
            ?? sessions[0],
        );
      }
      this.schedulePoll();
    } catch (error) {
      this.errorMessage.set(
        extractServerMessage(
          error,
          'Offline replay is unavailable. Check the Python data service and retry.',
        ),
      );
    } finally {
      this.loading.set(false);
    }
  }

  async launch(): Promise<void> {
    const sessionDateMs = this.selectedSessionDateMs();
    if (sessionDateMs === null || this.active()) return;
    this.acting.set(true);
    this.errorMessage.set(null);
    try {
      const session = await this.replay.createSession({
        session_date_ms: sessionDateMs,
        symbols: ['SPY', 'TSLA'],
        playback_minutes: this.playbackMinutes(),
        speed: this.speed(),
        initial_cash_usd: '100000',
        auto_fetch: true,
      });
      this.currentSession.set(session);
      this.recentSessions.update((sessions) => [session, ...sessions]);
      this.schedulePoll();
    } catch (error) {
      this.errorMessage.set(
        extractServerMessage(error, 'The offline replay could not be launched.'),
      );
    } finally {
      this.acting.set(false);
    }
  }

  async sendCommand(
    action: OfflineReplayCommandAction,
    speed?: OfflineReplaySpeed,
  ): Promise<void> {
    const session = this.currentSession();
    if (!session || this.acting()) return;
    this.acting.set(true);
    this.errorMessage.set(null);
    try {
      const updated = await this.replay.command(
        session.session_id,
        speed === undefined ? { action } : { action, speed },
      );
      this.currentSession.set(updated);
      if (speed !== undefined) this.speed.set(speed);
      this.schedulePoll();
    } catch (error) {
      this.errorMessage.set(
        extractServerMessage(error, 'The replay command was not accepted.'),
      );
    } finally {
      this.acting.set(false);
    }
  }

  selectDate(event: Event): void {
    const value = Number((event.target as HTMLSelectElement).value);
    this.selectedSessionDateMs.set(Number.isFinite(value) ? value : null);
  }

  selectDuration(event: Event): void {
    const value = Number((event.target as HTMLSelectElement).value);
    if (value === 30 || value === 60) this.playbackMinutes.set(value);
  }

  selectLaunchSpeed(event: Event): void {
    const value = Number((event.target as HTMLSelectElement).value);
    if (value === 1 || value === 10 || value === 60) this.speed.set(value);
  }

  cachedLabel(session: OfflineReplayCatalogSession): string {
    if (session.cached_symbols.length === 2) return 'SPY + TSLA cached';
    if (session.cached_symbols.length === 1) {
      return `${session.cached_symbols[0]} cached · missing symbol downloads on launch`;
    }
    return 'Downloads SPY + TSLA on launch';
  }

  private schedulePoll(): void {
    if (this.destroyed || !this.active() || this.pollHandle !== null) return;
    this.pollHandle = setTimeout(() => {
      this.pollHandle = null;
      void this.poll();
    }, 750);
  }

  private async poll(): Promise<void> {
    const sessionId = this.currentSession()?.session_id;
    if (!sessionId || this.destroyed) return;
    try {
      const updated = await this.replay.getSession(sessionId);
      this.currentSession.set(updated);
      this.recentSessions.update((sessions) =>
        sessions.map((session) =>
          session.session_id === updated.session_id ? updated : session
        ),
      );
    } catch (error) {
      this.errorMessage.set(
        extractServerMessage(error, 'Replay status could not be refreshed.'),
      );
    } finally {
      this.schedulePoll();
    }
  }
}
