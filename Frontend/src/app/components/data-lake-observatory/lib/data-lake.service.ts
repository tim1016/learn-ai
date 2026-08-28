import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import type {
  ArtifactDetail,
  BackfillDefaults,
  CoverageResponse,
  DataLakeDataType,
  DataLakeRead,
  PriceAdjustmentMode,
  StorageSummaryResponse,
} from './data-lake.types';

export interface CoverageQuery {
  readonly symbol: string;
  /** `YYYY-MM-DD`, the shape the coverage endpoint's `date` query params take. */
  readonly startTradingDate: string;
  readonly endTradingDate: string;
  readonly market?: string;
  readonly dataType?: DataLakeDataType;
  readonly priceAdjustmentMode?: PriceAdjustmentMode;
}

interface RejectionDetail {
  readonly reason?: unknown;
  readonly message?: unknown;
}

/**
 * Reads the flag-gated data-lake surface directly from the Python data
 * plane (`/api/data-lake/*` proxies straight through; only `/api/jobs` is
 * served by .NET).
 *
 * Every call resolves to a `DataLakeRead` rather than throwing, so the
 * "lake is dark" case (`DATA_LAKE_ENABLED` off → 404 on every route) is a
 * named outcome the page renders honestly instead of an unclassified error.
 */
@Injectable({ providedIn: 'root' })
export class DataLakeService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/data-lake`;

  async coverage(query: CoverageQuery): Promise<DataLakeRead<CoverageResponse>> {
    return this.read(() =>
      firstValueFrom(
        this.http.get<CoverageResponse>(`${this.base}/coverage`, {
          params: {
            symbol: query.symbol,
            start_trading_date: query.startTradingDate,
            end_trading_date: query.endTradingDate,
            market: query.market ?? 'usa',
            data_type: query.dataType ?? 'trade',
            price_adjustment_mode: query.priceAdjustmentMode ?? 'raw',
          },
        }),
      ),
    );
  }

  async artifact(artifactId: number): Promise<DataLakeRead<ArtifactDetail>> {
    return this.read(() =>
      firstValueFrom(this.http.get<ArtifactDetail>(`${this.base}/artifacts/${artifactId}`)),
    );
  }

  async storageSummary(market = 'usa'): Promise<DataLakeRead<StorageSummaryResponse>> {
    return this.read(() =>
      firstValueFrom(
        this.http.get<StorageSummaryResponse>(`${this.base}/storage-summary`, {
          params: { market },
        }),
      ),
    );
  }

  async backfillDefaults(market = 'usa'): Promise<DataLakeRead<BackfillDefaults>> {
    return this.read(() =>
      firstValueFrom(
        this.http.get<BackfillDefaults>(`${this.base}/backfill-defaults`, { params: { market } }),
      ),
    );
  }

  private async read<T>(request: () => Promise<T>): Promise<DataLakeRead<T>> {
    try {
      return { kind: 'ok', value: await request() };
    } catch (error) {
      return classifyDataLakeError(error);
    }
  }
}

/**
 * Maps a failed data-lake request onto its named outcome.
 *
 * 404 is the flag-off signature: `main.py` skips mounting the router
 * entirely when `DATA_LAKE_ENABLED` is false, so every route answers 404
 * with FastAPI's own body. `GET /artifacts/{id}` also 404s for an id the
 * catalog does not hold; both mean "there is nothing here to show", and the
 * artifact inspector says so with the artifact id in hand.
 */
export function classifyDataLakeError<T>(error: unknown): DataLakeRead<T> {
  if (!(error instanceof HttpErrorResponse)) {
    return { kind: 'unavailable', message: error instanceof Error ? error.message : String(error) };
  }
  if (error.status === 404) return { kind: 'not_enabled' };
  if (error.status === 422) {
    const detail = (error.error as { detail?: RejectionDetail } | null)?.detail;
    const reason = typeof detail?.reason === 'string' ? detail.reason : 'validation_failed';
    const message = typeof detail?.message === 'string' ? detail.message : error.message;
    return { kind: 'rejected', reason, message };
  }
  return {
    kind: 'unavailable',
    message: error.status === 0 ? 'The data plane did not respond.' : error.message,
  };
}
