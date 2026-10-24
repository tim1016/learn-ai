import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { AffectedOperation, ScopedHealth } from './scoped-health';

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
