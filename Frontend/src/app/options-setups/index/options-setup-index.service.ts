import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

type Schemas = components['schemas'];

export type OptionsSetupIndex = Schemas['OptionsSetupIndexResponse'];
export type IndexedRun = Schemas['IndexedRun'];
export type IndexedStudy = Schemas['IndexedStudy'];
export type IndexedApproval = Schemas['IndexedApproval'];

/**
 * The one read of Options evidence: every run, study and approved version, each
 * with the hash of the content it froze (#3512). The saved setups are a
 * different read (`OptionsBacktestService.list`), each with the hash of its own
 * content; the two meet by equal hash and never by name.
 */
@Injectable({ providedIn: 'root' })
export class OptionsSetupIndexService {
  private readonly http = inject(HttpClient);

  read(): Promise<OptionsSetupIndex> {
    return firstValueFrom(this.http.get<OptionsSetupIndex>('/api/research/options-backtest/setup-index'));
  }
}
