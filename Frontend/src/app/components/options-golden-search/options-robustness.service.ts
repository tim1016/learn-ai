import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';
import type { EvidenceWindow } from './options-golden-search.types';

export type CandidateRobustness = components['schemas']['OptionsCandidateRobustness'];
export type NeighborPage = components['schemas']['OptionsNeighborPage'];

@Injectable({ providedIn: 'root' })
export class OptionsRobustnessService {
  private readonly http = inject(HttpClient);
  private path(study: string, digest: string): string {
    return `/api/research/options-golden-search/studies/${encodeURIComponent(study)}/candidate-evidence/${encodeURIComponent(digest)}`;
  }

  read(study: string, digest: string, window: EvidenceWindow): Promise<CandidateRobustness> {
    return firstValueFrom(this.http.get<CandidateRobustness>(`${this.path(study, digest)}/robustness`, { params: { window } }));
  }

  neighbors(study: string, digest: string, window: EvidenceWindow, offset: number): Promise<NeighborPage> {
    return firstValueFrom(this.http.get<NeighborPage>(`${this.path(study, digest)}/neighbors`, { params: { window, offset, limit: 50 } }));
  }
}
