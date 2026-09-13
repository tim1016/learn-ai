import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';

/* DataLabPlanService — thin transport for POST /api/dataset/plan (PRD §12).
 *
 * Python authors the canonical column plan, session resolution, and
 * workload estimates. The Angular side renders the receipt unchanged and
 * performs NO client-side column/bar/weekday math. The request body is the
 * generate-zip-shaped payload produced by the pure request mapper, so plan
 * and generate can never drift apart. */

/** The plan receipt, rendered as-is. Fields are optional-by-contract so an
 *  evolving Python response never breaks rendering; unknown fields are
 *  ignored, never computed around. */
export interface DataLabPlanReceipt {
  session_count?: number;
  sessions?: readonly { open_ms_utc?: number; close_ms_utc?: number }[];
  output_column_count?: number;
  output_columns?: readonly string[];
  warnings?: readonly string[];
  timeframe_advice?: Record<string, unknown>;
  estimates?: Readonly<Record<string, unknown>>;
  estimate_assumptions?: Readonly<Record<string, unknown>>;
  estimate_provenance?: Record<string, unknown>;
  companion_dependencies?: readonly string[];
  exchange?: string;
  timezone?: string;
  calendar_version?: string;
  [key: string]: unknown;
}

@Injectable({ providedIn: 'root' })
export class DataLabPlanService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/dataset/plan`;

  /** Request the Python-authored dataset plan for the given recipe payload. */
  async plan(payload: Record<string, unknown>): Promise<DataLabPlanReceipt> {
    return firstValueFrom(
      this.http.post<DataLabPlanReceipt>(this.base, payload),
    );
  }
}
