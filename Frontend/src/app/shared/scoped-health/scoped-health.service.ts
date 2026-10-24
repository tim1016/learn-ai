import { HttpClient, HttpParams } from '@angular/common/http';
import { DestroyRef, inject, Injectable, resource, type ResourceRef } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { AffectedOperation, ScopedHealth } from './scoped-health';

/** How often a view of the projection reads it again while it is on screen. Every chip and notice takes its cadence from this one number. */
export const SCOPED_HEALTH_REFRESH_MS = 30_000;

/** What a reader wants to hear about. Neither: every finding. */
export interface ScopedHealthQuery {
  readonly operations?: readonly AffectedOperation[];
  readonly instrument?: string | null;
}

/** The one read of `GET /api/scoped-health`. The data plane filters; this only asks. */
@Injectable({ providedIn: 'root' })
export class ScopedHealthService {
  private readonly http = inject(HttpClient);

  read(query: ScopedHealthQuery = {}): Promise<ScopedHealth> {
    let params = new HttpParams();
    for (const operation of query.operations ?? []) params = params.append('operation', operation);
    // The data plane names symbols in upper case; a page's committed symbol is whatever was typed.
    const instrument = query.instrument?.trim().toUpperCase();
    if (instrument) params = params.set('instrument', instrument);
    return firstValueFrom(this.http.get<ScopedHealth>('/api/scoped-health', { params }));
  }
}

/**
 * A view of the projection that stays current: it reads when called, again
 * whenever `query` changes, and again every `SCOPED_HEALTH_REFRESH_MS` until
 * the caller is destroyed. Call it from an injection context (a field
 * initializer).
 *
 * A refresh keeps the last answer until the new one arrives, so a view does not
 * blink out on every tick. A refresh that fails leaves an error, not the last
 * answer: a finding that can no longer be confirmed is unknown, not still true.
 */
export function watchScopedHealth(query: () => ScopedHealthQuery = () => ({})): ResourceRef<ScopedHealth | undefined> {
  const health = inject(ScopedHealthService);
  const projection = resource({ params: query, loader: ({ params }) => health.read(params) });
  const timer = setInterval(() => projection.reload(), SCOPED_HEALTH_REFRESH_MS);
  inject(DestroyRef).onDestroy(() => clearInterval(timer));
  return projection;
}
