import { DestroyRef, Injectable, inject, signal } from '@angular/core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { BrokersService } from './brokers.service';

const POLL_INTERVAL_MS = 5000;

/**
 * Singleton owner of the Alpaca live verdict signal (ADR 0059 D8).
 *
 * Polls ``GET /api/brokers/alpaca/live-verdict`` every five seconds and
 * exposes the latest verdict as a signal. The shell renders the global
 * Alpaca account-mode banner from it. Mirrors ``BrokerHealthService`` for
 * IBKR; the two are separate because they answer separate questions
 * (market-data connection vs. real-money account mode).
 *
 * Never derive the mode from an env var or an account-id shape here: the
 * server's verdict is the only source of truth (ADR 0011 §7).
 */
@Injectable({ providedIn: 'root' })
export class AlpacaLiveVerdictService {
  private readonly brokers = inject(BrokersService);
  private readonly destroyRef = inject(DestroyRef);

  readonly verdict = signal<AlpacaLiveVerdict | null>(null);
  readonly lastError = signal<unknown | null>(null);

  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  constructor() {
    this.destroyRef.onDestroy(() => this.stop());
  }

  /** Begin background polling. Idempotent — the shell calls this once on boot. */
  start(): void {
    if (this.started) return;
    this.started = true;
    void this.refresh();
    this.pollTimer = setInterval(() => void this.refresh(), POLL_INTERVAL_MS);
  }

  async refresh(): Promise<void> {
    try {
      this.verdict.set(await this.brokers.getLiveVerdict());
      this.lastError.set(null);
    } catch (err) {
      // A failed read means the verdict is unknown to this client; never
      // keep rendering a stale "paper" over a network fault.
      this.lastError.set(err);
      this.verdict.set(null);
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
