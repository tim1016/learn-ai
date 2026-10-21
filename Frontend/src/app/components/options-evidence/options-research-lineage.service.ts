import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

export type ResearchLineage = components['schemas']['OptionsResearchLineage'];
export type ResearchTrialPage = components['schemas']['OptionsLineageTrialPage'];

@Injectable({ providedIn: 'root' })
export class OptionsResearchLineageService {
  private readonly http = inject(HttpClient);

  read(studyId: string): Promise<ResearchLineage> {
    return firstValueFrom(this.http.get<ResearchLineage>(`/api/research/options-golden-search/studies/${encodeURIComponent(studyId)}/lineage`));
  }

  trials(studyId: string, after: string): Promise<ResearchTrialPage> {
    return firstValueFrom(this.http.get<ResearchTrialPage>(`/api/research/options-golden-search/studies/${encodeURIComponent(studyId)}/lineage/trials`, {
      params: new HttpParams().set('after', after).set('limit', 50),
    }));
  }
}
