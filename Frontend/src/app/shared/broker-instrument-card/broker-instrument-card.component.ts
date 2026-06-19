import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Subject, debounceTime, distinctUntilChanged, switchMap } from 'rxjs';
import { BrokerService, type SymbolSearchSecType } from '../../services/broker.service';
import type { SymbolMatch } from '../../api/broker-models';

/**
 * Broker-coupled symbol picker (Slice 1F).
 *
 * Debounces the typed pattern and calls
 * ``broker.searchSymbols(q, secType)`` — the same IBKR ``reqMatchingSymbols``
 * proxy ``/broker/options-chain`` uses for its underlying-symbol search.
 * No non-broker fallback: if IBKR is disconnected the dropdown shows
 * "Reconnect broker to search" inline. Selecting a row emits the picked
 * match upward so the caller can write the operator-declared leg's
 * ``instrument.underlying`` from a symbol IBKR will actually qualify.
 */
const SEARCH_DEBOUNCE_MS = 500;

@Component({
  selector: 'app-broker-instrument-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-instrument-card.component.html',
  styleUrl: './broker-instrument-card.component.scss',
})
export class BrokerInstrumentCardComponent {
  readonly secType = input<SymbolSearchSecType | undefined>(undefined);
  readonly placeholder = input<string>('Search symbol or name…');
  readonly pick = output<SymbolMatch>();

  private readonly broker = inject(BrokerService);

  readonly query = signal<string>('');
  readonly matches = signal<SymbolMatch[]>([]);
  readonly searching = signal<boolean>(false);
  readonly disconnected = signal<boolean>(false);
  readonly rateLimited = signal<boolean>(false);

  readonly empty = computed<boolean>(
    () => !this.searching() && this.query().trim().length > 0 && this.matches().length === 0,
  );

  private readonly _queryStream = new Subject<string>();

  constructor() {
    this._queryStream
      .pipe(
        debounceTime(SEARCH_DEBOUNCE_MS),
        distinctUntilChanged(),
        switchMap((q) => this._search(q)),
        takeUntilDestroyed(inject(DestroyRef)),
      )
      .subscribe();
  }

  onQueryChange(value: string): void {
    this.query.set(value);
    this._queryStream.next(value);
  }

  select(match: SymbolMatch): void {
    this.pick.emit(match);
    // Collapse the dropdown without losing the typed text — the caller
    // displays the picked symbol; clearing the query would surprise the
    // operator if they immediately retype to refine.
    this.matches.set([]);
  }

  trackMatch = (_: number, m: SymbolMatch): string => `${m.symbol}::${m.exchange}`;

  private async _search(q: string): Promise<void> {
    const trimmed = q.trim();
    if (trimmed.length === 0) {
      this.matches.set([]);
      this.disconnected.set(false);
      this.rateLimited.set(false);
      return;
    }
    this.searching.set(true);
    this.disconnected.set(false);
    this.rateLimited.set(false);
    const issuedFor = trimmed;
    const issuedSecType = this.secType();
    try {
      const response = await this.broker.searchSymbols(trimmed, issuedSecType);
      // Drop the response if the operator has typed past this query (or
      // cleared it) while IBKR was responding. Without this guard the
      // dropdown could surface stale matches for a query the operator
      // no longer cares about.
      if (this.query().trim() !== issuedFor || this.secType() !== issuedSecType) return;
      this.matches.set(response.matches);
    } catch (err: unknown) {
      if (this.query().trim() !== issuedFor || this.secType() !== issuedSecType) return;
      this.matches.set([]);
      const status = (err as { status?: number })?.status ?? 0;
      if (status === 503) this.disconnected.set(true);
      else if (status === 429) this.rateLimited.set(true);
      // Other transport errors: silent — picker shows an empty dropdown,
      // operator can retry. Server logs the failure.
    } finally {
      if (this.query().trim() === issuedFor && this.secType() === issuedSecType) {
        this.searching.set(false);
      }
    }
  }
}
