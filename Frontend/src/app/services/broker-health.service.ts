import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';
import type { IbkrConnectionHealth } from '../api/broker-models';
import { BrokerService } from './broker.service';

const POLL_INTERVAL_MS = 5000;

/**
 * Singleton owner of the connection-health signal.
 *
 * Polls ``GET /api/broker/health`` every five seconds and exposes the
 * latest snapshot as a signal. The shell renders the global paper /
 * live / disconnected banner from this signal; per-page components
 * gate destructive actions (e.g. order placement) on
 * ``isPaperConnected()``.
 *
 * Per the IBKR integration plan: never derive the banner from the
 * ``IBKR_MODE`` env var. ``health.is_paper`` is the only source of
 * truth that survives misconfiguration because it reflects the
 * post-connect account-id sentinel check.
 */
@Injectable({ providedIn: 'root' })
export class BrokerHealthService {
  private readonly broker = inject(BrokerService);
  private readonly destroyRef = inject(DestroyRef);

  readonly health = signal<IbkrConnectionHealth | null>(null);
  readonly lastError = signal<unknown | null>(null);

  /**
   * The banner state derived from the latest health snapshot. ``null``
   * means we have not yet received a first response from the service
   * (loading / unknown — render no banner).
   */
  readonly bannerState = computed<'paper' | 'live' | 'disconnected' | null>(() => {
    const h = this.health();
    if (h === null) return null;
    if (!h.connected) return 'disconnected';
    return h.is_paper ? 'paper' : 'live';
  });

  /**
   * Defense-in-depth gate for any UI that places orders. Mirrors the
   * server-side third paper safety layer (DU account-id sentinel) so
   * the form stays locked until both sides agree.
   */
  readonly isPaperConnected = computed<boolean>(() => {
    const h = this.health();
    return h !== null && h.connected === true && h.is_paper === true;
  });

  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  constructor() {
    this.destroyRef.onDestroy(() => this.stop());
  }

  /**
   * Begin background polling. Idempotent — repeated calls are no-ops.
   * The shell calls this once on app boot.
   */
  start(): void {
    if (this.started) return;
    this.started = true;
    void this.refresh();
    this.pollTimer = setInterval(() => void this.refresh(), POLL_INTERVAL_MS);
  }

  /**
   * Force a synchronous refresh outside the poll cadence. Used by the
   * Status page's manual "Refresh" button.
   */
  async refresh(): Promise<void> {
    try {
      const h = await this.broker.health();
      this.health.set(h);
      this.lastError.set(null);
    } catch (err) {
      // /health is documented to never raise on the server; an error
      // here means the network or proxy is down. Surface it without
      // crashing the poll.
      this.lastError.set(err);
      this.health.set(null);
    }
  }

  private stop(): void {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.started = false;
  }
}
