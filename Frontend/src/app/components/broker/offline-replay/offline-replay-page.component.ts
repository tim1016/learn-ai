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
  OfflineReplayCommandAction,
  OfflineReplaySession,
  OfflineReplaySpeed,
} from '../../../api/offline-replay.types';
import { OfflineReplayService } from '../../../services/offline-replay.service';
import { formatTimestampDisplay } from '../../../shared/timestamp/timestamp-display';
import { extractServerMessage } from '../operation-error';
import {
  OfflineReplayDateOption,
  OfflineReplayLaunchCardComponent,
} from './offline-replay-launch-card.component';
import { OfflineReplaySessionCardComponent } from './offline-replay-session-card.component';

const ACTIVE_STATUSES = new Set<OfflineReplaySession['status']>([
  'preparing',
  'warming_up',
  'running',
  'paused',
  'stopping',
]);

@Component({
  selector: 'app-offline-replay-page',
  imports: [OfflineReplayLaunchCardComponent, OfflineReplaySessionCardComponent],
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
  readonly currentSession = signal<OfflineReplaySession | null>(null);
  readonly selectedSessionDateMs = signal<number | null>(null);
  readonly playbackMinutes = signal<30 | 60>(60);
  readonly speed = signal<OfflineReplaySpeed>(60);
  readonly loading = signal(true);
  readonly acting = signal(false);
  readonly errorMessage = signal<string | null>(null);

  readonly dateOptions = computed<OfflineReplayDateOption[]>(() =>
    (this.catalog()?.sessions ?? [])
      .filter((session) => session.eligible)
      .map((session) => ({
        session,
        label: formatTimestampDisplay(session.session_date_ms, { mode: 'date-et' }),
      })),
  );

  readonly active = computed(() => {
    const status = this.currentSession()?.status;
    return status !== undefined && ACTIVE_STATUSES.has(status);
  });

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.destroyed = true;
      if (this.pollHandle !== null) clearTimeout(this.pollHandle);
    });
    afterNextRender(() => void this.initialize());
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
      this.selectedSessionDateMs.set(
        catalog.recommended_session_date_ms
          ?? catalog.sessions.find((session) => session.eligible)?.session_date_ms
          ?? null,
      );
      if (sessions.length > 0) {
        this.currentSession.set(
          sessions.find((session) => ACTIVE_STATUSES.has(session.status)) ?? sessions[0],
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
      this.currentSession.set(await this.replay.getSession(sessionId));
    } catch (error) {
      this.errorMessage.set(
        extractServerMessage(error, 'Replay status could not be refreshed.'),
      );
    } finally {
      this.schedulePoll();
    }
  }
}
