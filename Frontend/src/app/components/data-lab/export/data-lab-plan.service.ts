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

/** The plan receipt, rendered as-is. Mirrors the Python DatasetPlanResponse
 *  contract (PythonDataService/app/schemas/dataset_plan.py and the generated
 *  components.schemas.DatasetPlanResponse snapshot). Fields are
 *  optional-by-contract so an evolving Python response never breaks
 *  rendering; unknown fields are ignored, never computed around. */
export interface DataLabPlanReceipt {
  ticker?: string;
  window_start_ms_utc?: number;
  window_end_ms_utc?: number;
  /** Session entries are YYYY-MM-DD strings today; the server is moving to
   *  ms-UTC session anchors, so render defensively (see ExportComponent). */
  exchange_sessions?: readonly unknown[];
  session_count?: number;
  output_columns?: readonly string[];
  output_column_count?: number;
  /** Header of dataset.csv's readable time column for the requested zone. */
  time_column?: string | null;
  estimated_bars?: number;
  estimate_assumptions?: readonly string[];
  estimate_provenance?: string;
  companion_dependencies?: readonly string[];
  warnings?: readonly string[];
  allowed_timeframes?: readonly string[];
  recommended_timeframes?: readonly string[];
  exchange?: string;
  calendar_timezone?: string;
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
