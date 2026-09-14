import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../environments/environment';

/* DataLabRangePresetsService — thin transport for GET /api/chart/range-presets.
 *
 * Python owns every date decision: each preset is the last N scheduled NYSE
 * sessions, resolved by the canonical trading calendar. Angular applies the
 * resolved dates verbatim and computes nothing — no client-side holiday
 * tables, no calendar-day arithmetic. Responses are cached briefly so a chip
 * row doesn't refetch per click while still tracking "now" across a long-lived
 * tab. */

/** One calendar-resolved quick range, exactly as Python resolved it. */
export interface ChartRangePreset {
  key: string;
  label: string;
  /** YYYY-MM-DD trading dates, inclusive. */
  start_date: string;
  end_date: string;
  /** UTC-midnight anchors of the trading dates above — the Data Lab window
   *  convention, applied to the store unchanged. */
  start_ms_utc: number;
  end_ms_utc: number;
  session_count: number;
  estimated_bars_per_timeframe: Record<string, number>;
}

interface PresetsResponse {
  presets: ChartRangePreset[];
}

function isChartRangePreset(value: unknown): value is ChartRangePreset {
  if (typeof value !== 'object' || value === null) return false;
  const preset = value as Record<string, unknown>;
  return typeof preset['key'] === 'string'
    && typeof preset['label'] === 'string'
    && typeof preset['start_date'] === 'string'
    && typeof preset['end_date'] === 'string'
    && typeof preset['start_ms_utc'] === 'number'
    && Number.isFinite(preset['start_ms_utc'])
    && typeof preset['end_ms_utc'] === 'number'
    && Number.isFinite(preset['end_ms_utc'])
    && typeof preset['session_count'] === 'number';
}

const CACHE_TTL_MS = 5 * 60_000;

@Injectable({ providedIn: 'root' })
export class DataLabRangePresetsService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/chart/range-presets`;
  private readonly cache = new Map<string, { at: number; presets: readonly ChartRangePreset[] }>();
  private readonly inflight = new Map<string, Promise<readonly ChartRangePreset[]>>();

  /** Fetch the calendar-resolved presets for a session policy (estimates are
   *  session-dependent server-side). Served from cache within the TTL. */
  async presets(session: 'rth' | 'extended' = 'rth'): Promise<readonly ChartRangePreset[]> {
    const cached = this.cache.get(session);
    if (cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.presets;
    const pending = this.inflight.get(session);
    if (pending) return pending;

    const request = firstValueFrom(
      this.http.get<PresetsResponse>(this.base, { params: { session } }),
    )
      .then((response) => {
        const raw = response?.presets;
        const presets = Array.isArray(raw) ? raw.filter(isChartRangePreset) : [];
        this.cache.set(session, { at: Date.now(), presets });
        return presets;
      })
      .finally(() => this.inflight.delete(session));
    this.inflight.set(session, request);
    return request;
  }
}
