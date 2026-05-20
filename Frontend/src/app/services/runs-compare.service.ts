import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type { CompareResponse } from '../models/compare-response';

/**
 * PR B (2026-05-19) Phase 4 — REST client for the Backend's compare
 * endpoint at ``GET /api/runs/compare?left=&right=``.
 *
 * The service is a thin pass-through: the .NET ``CompareController``
 * already shapes the response per spec § 6.5, so the only job here is to
 * route the two ids into URL query params and surface the typed result
 * back to the component.  Errors propagate through to the consumer
 * untouched — the compare view's ``resource()`` already handles the
 * loading/error states.
 */
@Injectable({ providedIn: 'root' })
export class RunsCompareService {
  private readonly http = inject(HttpClient);
  private readonly base = `${this.resolveBackendBase()}/api/runs`;

  getCompare(left: number, right: number): Observable<CompareResponse> {
    const params = new HttpParams()
      .set('left', String(left))
      .set('right', String(right));
    return this.http.get<CompareResponse>(`${this.base}/compare`, { params });
  }

  /** Strip the trailing ``/graphql`` from the environment URL so the REST
   * endpoint can sit alongside the GraphQL endpoint on the same backend. */
  private resolveBackendBase(): string {
    const url = environment.backendUrl;
    return url.endsWith('/graphql') ? url.slice(0, -'/graphql'.length) : url;
  }
}
