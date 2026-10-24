import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../environments/environment';
import type { components } from '../../api/broker.types';

/** One recorded run of a study, as the data plane stored it when the run ended (#3520). */
export type StoredStudyRecord = components['schemas']['StoredStudyRecord'];

/** The History kinds that are a study's own runs: the `type` of an entry and the segment of its detail address. */
export type StoredStudyType = StoredStudyRecord['study'];

/** Closed over every kind the server sends, so a study added there cannot ship without being named here. */
export const STORED_STUDY_TYPES: Readonly<Record<StoredStudyType, true>> = {
  'feature-study': true,
  'signal-study': true,
  'cross-sectional-study': true,
  'indicator-reliability': true,
  'realized-vs-iv': true,
};

export const STORED_STUDY_TYPE_IDS = Object.keys(STORED_STUDY_TYPES) as StoredStudyType[];

/** No run of this study is saved under the id. */
export class StoredStudyNotFoundError extends Error {
  constructor(
    readonly study: StoredStudyType,
    readonly id: string,
  ) {
    super(`No ${study} run is saved as ${id}.`);
  }
}

/** The data plane holds the record but cannot read it (a newer build wrote it, or its files are damaged). */
export class StoredStudyUnreadableError extends Error {
  constructor(readonly reason: string) {
    super(reason);
  }
}

/**
 * Saved study runs, read on demand. Reading a record starts nothing: there is no write on this service, and the
 * endpoint it calls has none either. A rerun is the study page's own, explicit action.
 */
@Injectable({ providedIn: 'root' })
export class StoredStudyService {
  private readonly http = inject(HttpClient);
  private readonly url = `${environment.pythonServiceUrl}/api/research/study-records`;

  async read(study: StoredStudyType, id: string): Promise<StoredStudyRecord> {
    try {
      return await firstValueFrom(this.http.get<StoredStudyRecord>(`${this.url}/${study}/${encodeURIComponent(id)}`));
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 404) throw new StoredStudyNotFoundError(study, id);
      const detail = error instanceof HttpErrorResponse && error.status === 500 ? error.error?.detail : null;
      if (detail?.code === 'STUDY_RECORD_UNREADABLE') throw new StoredStudyUnreadableError(String(detail.message));
      throw error;
    }
  }
}
