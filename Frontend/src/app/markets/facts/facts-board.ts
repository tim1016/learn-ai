import { signal } from '@angular/core';

import { MAX_FACT_SYMBOLS, type InstrumentFacts, type InstrumentFactsService } from './instrument-facts.service';

/** One symbol's facts as a screen reads them: asked and not answered yet, answered, or not readable. */
export type FactState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly facts: InstrumentFacts }
  | { readonly kind: 'failed'; readonly message: string };

const LOADING: FactState = { kind: 'loading' };

/**
 * The facts of the rows a list has drawn, asked for in bounded batches.
 *
 * `ask` reads each symbol once: a symbol already asked for, answered or not, is
 * left alone, so drawing more rows never re-reads the ones on screen, and a
 * list that redraws asks for nothing. A batch is one request however many rows
 * it names; the server's bound on a batch is the largest one sent. `retry`
 * asks again for the symbols whose read failed, and only those.
 */
export class FactsBoard {
  private readonly states = signal<ReadonlyMap<string, FactState>>(new Map());

  constructor(private readonly service: Pick<InstrumentFactsService, 'read'>) {}

  /** The symbol's facts. A symbol never asked for reads as loading. */
  stateOf(symbol: string): FactState {
    return this.states().get(symbol) ?? LOADING;
  }

  /** Whether any asked symbol's read failed. */
  hasFailed(): boolean {
    return [...this.states().values()].some((state) => state.kind === 'failed');
  }

  ask(symbols: readonly string[]): void {
    const unasked = symbols.filter((symbol) => !this.states().has(symbol));
    if (unasked.length > 0) this.send(unasked);
  }

  retry(): void {
    const failed = [...this.states()].flatMap(([symbol, state]) => (state.kind === 'failed' ? [symbol] : []));
    if (failed.length > 0) this.send(failed);
  }

  private send(symbols: readonly string[]): void {
    this.put(symbols, () => LOADING);
    for (let start = 0; start < symbols.length; start += MAX_FACT_SYMBOLS) {
      const batch = symbols.slice(start, start + MAX_FACT_SYMBOLS);
      void this.service.read(batch).then((read) =>
        this.put(batch, (symbol) => {
          if (read.kind === 'failed') return { kind: 'failed', message: read.message };
          const facts = read.facts.get(symbol);
          return facts === undefined
            ? { kind: 'failed', message: `The answer left ${symbol} out.` }
            : { kind: 'ready', facts };
        }),
      );
    }
  }

  private put(symbols: readonly string[], state: (symbol: string) => FactState): void {
    this.states.update((current) => {
      const next = new Map(current);
      for (const symbol of symbols) next.set(symbol, state(symbol));
      return next;
    });
  }
}
