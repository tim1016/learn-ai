import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

type Schemas = components['schemas'];

export type OptionsSetupIndex = Schemas['OptionsSetupIndexResponse'];
export type IndexedSetup = Schemas['IndexedSetup'];
export type IndexedRun = Schemas['IndexedRun'];
export type IndexedStudy = Schemas['IndexedStudy'];
export type IndexedApproval = Schemas['IndexedApproval'];

/**
 * The one read behind the Options setups index and each setup's Overview: the
 * saved setups beside every run, study and approved version, each with the
 * hash of the content it froze (#3512).
 */
@Injectable({ providedIn: 'root' })
export class OptionsSetupIndexService {
  private readonly http = inject(HttpClient);

  read(): Promise<OptionsSetupIndex> {
    return firstValueFrom(this.http.get<OptionsSetupIndex>('/api/research/options-backtest/setup-index'));
  }
}
