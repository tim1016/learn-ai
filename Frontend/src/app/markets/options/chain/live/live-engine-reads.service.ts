import { Injectable, inject, signal } from '@angular/core';

import type { IbkrChainSnapshot } from '../../../../api/broker-models';
import { QuantLibService } from '../../../../services/quantlib.service';
import { errorMessage } from '../../../../shared/errors/error-message';
import {
  contractKey,
  engineInputOf,
  sameEngineInput,
  type CrossCheckContext,
  type EngineAnswer,
  type EngineInput,
} from './live-cross-check';

/**
 * The engine's side of the live cross-check: one `QuantLibService.priceOption` read per contract,
 * asked again when what it is valued with has moved. Provided by the chain page.
 *
 * Only the answer to the question last asked of a contract is kept. A new question removes the
 * old answer at once, so a delta is never held against a spot or volatility it was not priced
 * with; a reply to a question that has since been replaced, or to a chain that has since been left
 * (`reset`), is dropped when it arrives. A contract that cannot be compared has no question and
 * no answer, and one that failed is asked again on the next snapshot.
 */
@Injectable()
export class LiveEngineReads {
  private readonly quantlib = inject(QuantLibService);
  /** The question last asked of each contract, in flight or answered. */
  private readonly asked = new Map<string, EngineInput>();
  private readonly held = signal<ReadonlyMap<string, EngineAnswer>>(new Map());
  /** Counts chains: a reply from an earlier one is never kept. */
  private generation = 0;

  /** The engine's answer for each contract, by `contractKey`. */
  readonly answers = this.held.asReadonly();

  /** Asks the engine about every contract of this snapshot that can be compared and whose question has changed. */
  offer(snapshot: IbkrChainSnapshot, context: CrossCheckContext): void {
    const generation = this.generation;
    for (const quote of snapshot.quotes) {
      const key = contractKey(quote.strike, quote.right);
      const ready = engineInputOf(quote, context);
      if (ready.kind === 'not_compared') {
        this.forget(key);
        continue;
      }
      const last = this.asked.get(key);
      if (last !== undefined && sameEngineInput(last, ready.input)) continue;
      this.asked.set(key, ready.input);
      this.dropDone(key);
      void this.read(key, ready.input, generation);
    }
  }

  /** A different chain: every question and answer of the last one goes, and a reply still in flight for it will be dropped. */
  reset(): void {
    this.generation++;
    this.asked.clear();
    this.held.set(new Map());
  }

  private forget(key: string): void {
    this.asked.delete(key);
    this.update((answers) => answers.delete(key));
  }

  private dropDone(key: string): void {
    if (this.held().get(key)?.kind === 'done') this.update((answers) => answers.delete(key));
  }

  private update(change: (answers: Map<string, EngineAnswer>) => unknown): void {
    this.held.update((current) => {
      const next = new Map(current);
      change(next);
      return next;
    });
  }

  private async read(key: string, input: EngineInput, generation: number): Promise<void> {
    const current = () => generation === this.generation && this.asked.get(key) === input;
    const fail = (why: string) => {
      this.asked.delete(key);
      this.update((answers) => answers.set(key, { kind: 'failed', input, why }));
    };
    try {
      const result = await this.quantlib.priceOption({
        spot: input.spot,
        strike: input.strike,
        volatility: input.iv,
        expirationMs: input.expirationMs,
        optionType: input.right === 'C' ? 'call' : 'put',
        engine: input.engine,
      });
      if (!current()) return;
      if (!result.success) fail(result.error ?? 'The engine did not return a comparison.');
      else this.update((answers) => answers.set(key, { kind: 'done', input, delta: result.delta, gamma: result.gamma }));
    } catch (error) {
      if (current()) fail(errorMessage(error));
    }
  }
}
