import { signal } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import type { RangeInstants } from './duration';
import { RunRefusedError } from './options-backtest.service';
import type { DataCheck, SetupRequest } from './options-backtest.types';

/** What the setup and the dates were when a check was asked, to tell later whether its answer still describes them. */
export interface CheckAsked extends RangeInstants {
  readonly stamp: string;
}

export type CheckState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'checking' }
  | { readonly kind: 'ready'; readonly answer: DataCheck; readonly asked: CheckAsked }
  | { readonly kind: 'failed'; readonly message: string };

const IDLE: CheckState = { kind: 'idle' };

/**
 * Asks the server which sessions of a range can be tested, and keeps the
 * answer until it is dismissed. Every ask carries a generation: an answer
 * lands only if no newer ask or dismissal has started since. A range the
 * server refuses is not a failure of the check: its message is kept apart, to
 * be shown at the dates. `pricesFromMs` outlives the panel: it is the day
 * option prices are available from, as the last answer said it.
 */
export class DataCheckFlow {
  private readonly current = signal<CheckState>(IDLE);
  readonly state = this.current.asReadonly();
  private readonly prices = signal<number | null>(null);
  readonly pricesFromMs = this.prices.asReadonly();
  private readonly refused = signal<string | null>(null);
  readonly refusal = this.refused.asReadonly();

  private generation = 0;

  constructor(private readonly check: (setup: SetupRequest, startMs: number, endMs: number) => Promise<DataCheck>) {}

  async run(setup: SetupRequest, range: RangeInstants, stamp: string): Promise<void> {
    this.generation += 1;
    const generation = this.generation;
    this.refused.set(null);
    this.current.set({ kind: 'checking' });
    let next: CheckState;
    try {
      const answer = await this.check(setup, range.start_ms, range.end_ms);
      if (generation === this.generation) this.prices.set(answer.option_prices_from_ms);
      next = { kind: 'ready', answer, asked: { ...range, stamp } };
    } catch (error) {
      if (generation !== this.generation) return;
      if (error instanceof RunRefusedError && error.status === 400) {
        this.refused.set(error.message);
        this.current.set(IDLE);
        return;
      }
      const message = error instanceof RunRefusedError ? error.message : extractServerMessage(error, 'The data could not be checked. Try again.');
      next = { kind: 'failed', message };
    }
    if (generation === this.generation) this.current.set(next);
  }

  /** Closes the panel; an answer still on its way is dropped. */
  dismiss(): void {
    this.generation += 1;
    this.current.set(IDLE);
  }

  /** The dates changed, so what was said about them no longer applies. */
  clearRefusal(): void {
    this.refused.set(null);
  }
}
