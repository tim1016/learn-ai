import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../environments/environment';
import type { components } from '../../api/broker.types';
import { failureAsError } from '../../services/python-http-failure';
import { errorMessage } from '../../shared/errors/error-message';

type Schemas = components['schemas'];
export type InstrumentFacts = Schemas['InstrumentFacts'];
export type InstrumentQuote = InstrumentFacts['quote'];
export type AvailableQuote = Schemas['AvailableQuote'];
export type OptionsArchiveFact = Schemas['OptionsArchiveFact'];
export type QuoteBasis = AvailableQuote['basis'];

/** The most symbols one read names; the server refuses a longer list, so a caller splits it. */
export const MAX_FACT_SYMBOLS = 25;

/** What one read of the facts came back with: every symbol's facts, or the one reason none could be read. */
export type FactsRead =
  | { readonly kind: 'ok'; readonly facts: ReadonlyMap<string, InstrumentFacts> }
  | { readonly kind: 'failed'; readonly message: string };

/**
 * Reads an instrument's header facts (PRD #3490 R08): its latest price with the
 * basis and instant it was observed, the day move with the close it is measured
 * from, and the options-archive status. Python computes them; this only asks.
 *
 * One request names a bounded batch of symbols, so a list page costs one read
 * however many rows it draws. It opens no subscription. A failed read resolves
 * as `failed` rather than rejecting, so the page that asked still opens.
 */
@Injectable({ providedIn: 'root' })
export class InstrumentFactsService {
  private readonly http = inject(HttpClient);

  async read(symbols: readonly string[]): Promise<FactsRead> {
    if (symbols.length === 0 || symbols.length > MAX_FACT_SYMBOLS) {
      throw new RangeError(`One read names between 1 and ${MAX_FACT_SYMBOLS} symbols; this one named ${symbols.length}.`);
    }
    try {
      const response = await firstValueFrom(
        this.http
          .get<Schemas['InstrumentFactsResponse']>(`${environment.pythonServiceUrl}/api/markets/instrument-facts`, {
            params: { symbols: symbols.join(',') },
          })
          .pipe(failureAsError()),
      );
      return { kind: 'ok', facts: new Map(response.facts.map((facts) => [facts.symbol, facts])) };
    } catch (error) {
      return { kind: 'failed', message: errorMessage(error) };
    }
  }
}
