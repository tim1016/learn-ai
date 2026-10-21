import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

export type EvidenceWorkspace = components['schemas']['OptionsEvidenceWorkspace'];

@Injectable({ providedIn: 'root' })
export class OptionsEvidenceService {
  private readonly http = inject(HttpClient);

  read(kind: string, id: string, inspectedStudy?: string, comparatorRun?: string): Promise<EvidenceWorkspace> {
    let params = new HttpParams();
    if (inspectedStudy) params = params.set('inspected_study_id', inspectedStudy);
    if (comparatorRun) params = params.set('comparator_run_id', comparatorRun);
    return firstValueFrom(this.http.get<EvidenceWorkspace>(
      `/api/research/options-golden-search/workspace/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, { params }));
  }
}
