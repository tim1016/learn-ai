import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';
import type { IbkrConnectionHealth } from '../api/broker-models';
import { BrokerService } from './broker.service';

const POLL_INTERVAL_MS = 5000;

export type LifecycleAction = 'connect' | 'disconnect' | 'reconnect';

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
   * Which lifecycle action (connect / disconnect / reconnect) is in
   * flight. Both the global banner and the Broker Status page read this
   * — clicking either control disables both, so two concurrent
   * connectAsyncs can't race past the server-side asyncio lock.
   */
  readonly lifecycleAction = signal<LifecycleAction | null>(null);
  readonly lifecycleError = signal<unknown | null>(null);

  /**
   * The banner state derived from the latest health snapshot. ``null``
   * means we have not yet received a first response from the service
   * (loading / unknown — render no banner).
   */
  readonly bannerState = computed<'paper' | 'live' | 'disconnected' | 'disabled-host-runner-active' | null>(() => {
    const h = this.health();
    if (h === null) return null;
    if (h.disabled === true) return 'disabled-host-runner-active';
    if (!h.connected) return 'disconnected';
    return h.is_paper ? 'paper' : 'live';
  });

  /**
   * Defense-in-depth gate for any UI that places orders. Mirrors the
   * server-side third paper safety layer (DU account-id sentinel) so
   * the form stays locked until both sides agree.
   *
   * Reads ``connection_state`` rather than the legacy ``connected``
   * boolean: during a TWS 1100 soft loss the socket is still up
   * (``connected=true``) but the feed is dead — order submission
   * would silently land on nothing. Codex P1 on PR #563.
   */
  readonly isPaperConnected = computed<boolean>(() => {
    const h = this.health();
    return h !== null && h.connection_state === 'connected' && h.is_paper === true;
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

  /**
   * Drive ``POST /api/broker/connect`` and refresh health on completion.
   * Shared by the global banner and the Broker Status page so a click
   * in either place is the same lifecycle action — and the
   * ``lifecycleAction`` signal locks both controls together.
   */
  connect(): Promise<void> {
    return this.runLifecycleAction('connect', () => this.broker.connect());
  }

  /** Drive ``POST /api/broker/disconnect`` and refresh health. */
  disconnect(): Promise<void> {
    return this.runLifecycleAction('disconnect', () => this.broker.disconnect());
  }

  /** Drive ``POST /api/broker/reconnect`` and refresh health. */
  reconnect(): Promise<void> {
    return this.runLifecycleAction('reconnect', () => this.broker.reconnect());
  }

  private async runLifecycleAction(
    action: LifecycleAction,
    call: () => Promise<unknown>,
  ): Promise<void> {
    if (this.lifecycleAction() !== null) return;
    this.lifecycleAction.set(action);
    this.lifecycleError.set(null);
    try {
      await call();
    } catch (err) {
      this.lifecycleError.set(err);
    } finally {
      this.lifecycleAction.set(null);
      await this.refresh();
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
