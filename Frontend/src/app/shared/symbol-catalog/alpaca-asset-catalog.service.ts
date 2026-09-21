import { HttpClient } from '@angular/common/http';
import { Injectable, Injector, computed, inject, resource } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../environments/environment';
import { classifyDataLakeError } from '../data-lake';
import type { DataLakeRead } from '../data-lake';

/**
 * One row of `GET /api/brokers/alpaca/symbols` — the trimmed picker
 * projection the broker router serves from its TTL-cached vendor read.
 * Wire fields stay snake_case; nothing here is recomputed, only filtered.
 */
export interface AlpacaSymbolEntry {
  readonly symbol: string;
  readonly name: string | null;
  readonly asset_class: string;
  readonly exchange: string | null;
  readonly status: string;
  readonly tradable: boolean;
}

/**
 * The only asset class the lake can backfill: `market='usa'` equities.
 * Alpaca's catalog also lists crypto and other classes — offering one of
 * those would promise a symbol the ensure-coverage gate could never deliver,
 * so the picker pool drops them at the join, not per surface.
 */
export const LAKE_BACKFILLABLE_ASSET_CLASS = 'us_equity';

/**
 * The live membership half of the shared symbol picker (ADR — symbol picker,
 * 2026-09-20): every listed US symbol is offerable, and the lake's coverage
 * of it is a per-row fact the picker renders — not a filter on the menu.
 *
 * One cached read per tab, shared by every picker on the page, exactly like
 * {@link TickerCatalogService} on the lake side. A failed read is a named
 * outcome (`unavailable`), never a fallback list: the joined view downgrades
 * to lake holdings *visibly* (the card shows the degraded banner) rather
 * than pretending the vendor answered.
 */
@Injectable({ providedIn: 'root' })
export class AlpacaAssetCatalogService {
  private readonly http = inject(HttpClient);
  private readonly injector = inject(Injector);
  private readonly loader = resource({
    loader: () => this.fetchCatalog(),
    injector: this.injector,
  });

  /** Every vendor row (active and inactive), or `null` while loading/dark. */
  readonly entries = computed<readonly AlpacaSymbolEntry[] | null>(() => {
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

  private async fetchCatalog(): Promise<DataLakeRead<readonly AlpacaSymbolEntry[]>> {
    try {
      const rows = await firstValueFrom(
        this.http.get<readonly AlpacaSymbolEntry[]>(
          `${environment.pythonServiceUrl}/api/brokers/alpaca/symbols`,
        ),
      );
      return { kind: 'ok', value: rows };
    } catch (error) {
      return classifyDataLakeError(error);
    }
  }
}
