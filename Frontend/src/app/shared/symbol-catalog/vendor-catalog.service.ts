import { HttpClient } from '@angular/common/http';
import { Injectable, Injector, computed, inject, resource } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';
import { classifyDataLakeError } from '../data-lake';
import type { DataLakeRead } from '../data-lake';

/**
 * One row of `GET /api/tickers/catalog` — the trimmed picker projection the
 * data-plane core serves from its TTL-cached Polygon reference walk. Aliased
 * to the generated contract type so a backend field rename breaks
 * compilation here instead of drifting silently at runtime; the wire stays
 * snake_case and nothing is recomputed, only filtered.
 */
export type VendorSymbolEntry = components['schemas']['SymbolCatalogEntry'];

/**
 * The only asset class the lake can backfill: `market='usa'` equities. The
 * catalog walk is `market="stocks"` server-side, so everything it serves
 * carries this class — the constant exists for the client-side filters that
 * must stay honest if the walk ever widens.
 */
export const LAKE_BACKFILLABLE_ASSET_CLASS = 'us_equity';

/**
 * The live membership half of the shared symbol picker (ADR 0066): every
 * listed US symbol is offerable, and the lake's coverage of it is a per-row
 * fact the picker renders — not a filter on the menu.
 *
 * The catalog is served by the data-plane core (the browser's ingress in the
 * split fleet) from a Polygon reference walk — a listing universe is market
 * reference data, and the coordinator must construct no provider broker
 * client (FR-041). Order-time eligibility stays with the order path; nothing
 * here claims a symbol is currently tradable.
 *
 * One cached read per tab, shared by every picker on the page, exactly like
 * {@link TickerCatalogService} on the lake side. A failed read is a named
 * outcome (`unavailable`), never a fallback list: the joined view downgrades
 * to lake holdings *visibly* (the card shows the degraded banner) rather
 * than pretending the vendor answered.
 */
@Injectable({ providedIn: 'root' })
export class VendorCatalogService {
  private readonly http = inject(HttpClient);
  private readonly injector = inject(Injector);
  private readonly loader = resource({
    loader: () => this.fetchCatalog(),
    injector: this.injector,
  });

  /** Every vendor row (active and inactive), or `null` while loading/dark. */
  readonly entries = computed<readonly VendorSymbolEntry[] | null>(() => {
    const read = this.loader.value();
    return read?.kind === 'ok' ? read.value : null;
  });

  readonly loading = computed(() => this.loader.isLoading());

  /**
   * Why the catalog is dark, once the read has settled on a failure. `null`
   * while in flight or answered — same verdict semantics the lake view uses.
   */
  readonly unavailable = computed<string | null>(() => {
    if (this.loader.isLoading()) return null;
    const read = this.loader.value();
    if (read === undefined || read.kind === 'ok') return null;
    return read.kind === 'rejected' ? `${read.reason}: ${read.message}` : read.message;
  });

  readonly reload = (): void => {
    this.loader.reload();
  };

  private async fetchCatalog(): Promise<DataLakeRead<readonly VendorSymbolEntry[]>> {
    try {
      // Relative on purpose: `/api/tickers` is served behind the same
      // browser proxy as every other data-plane read, in every topology.
      const rows = await firstValueFrom(
        this.http.get<readonly VendorSymbolEntry[]>('/api/tickers/catalog'),
      );
      return { kind: 'ok', value: rows };
    } catch (error) {
      return classifyDataLakeError(error);
    }
  }
}
