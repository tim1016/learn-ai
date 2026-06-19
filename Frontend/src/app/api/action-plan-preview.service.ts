/**
 * Thin client for ``POST /api/live-instances/preview-action-plan``.
 *
 * Pydantic is the authoritative validator (server-side). This service
 * just relays the candidate plan and surfaces the warning list; the
 * picker debounces the call and renders warnings inline above submit.
 *
 * Slice 1D (#597). Replaces the local TS validator that lived
 * transiently in Slice 1B and was correctly removed by thermo review
 * (commit b61beaed) once it became clear the right design was a
 * preview-endpoint wrapper, not a parallel Pydantic re-implementation.
 */

import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type { ActionPlan } from './action-plan.types';

export interface ParityWarning {
  code: 'orphan_entry';
  message: string;
  leg_id: string | null;
}

export interface ActionPlanPreviewResponse {
  warnings: ParityWarning[];
}

@Injectable({ providedIn: 'root' })
export class ActionPlanPreviewService {
  private readonly http = inject(HttpClient);

  preview(plan: ActionPlan): Promise<ActionPlanPreviewResponse> {
    return firstValueFrom(
      this.http.post<ActionPlanPreviewResponse>(
        '/api/live-instances/preview-action-plan',
        plan,
      ),
    );
  }
}
