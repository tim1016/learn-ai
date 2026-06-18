import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import paritySymmetricJson from '../../testing/action_plan_fixtures/parity_symmetric.json';
import parityOrphanJson from '../../testing/action_plan_fixtures/parity_orphan_entry.json';
import type { ActionPlan } from './action-plan.types';
import {
  ActionPlanPreviewService,
  type ActionPlanPreviewResponse,
} from './action-plan-preview.service';

// The JSON imports widen to literal types; cast to the canonical
// ``ActionPlan`` shape so the spec exercises the same surface the
// picker calls. Pydantic validates authoritatively server-side; the
// cast is the boundary-crossing acknowledgement, not a type weakening.
const paritySymmetric = paritySymmetricJson as unknown as ActionPlan;
const parityOrphan = parityOrphanJson as unknown as ActionPlan;

/** Cross-language fixture parity for Slice 1D. Same JSON files the
 * Python pure-function test exercises via Pydantic + ``parity_diagnostics``.
 * Frontend asserts that the shape sent over the wire is round-trip
 * stable and the response envelope matches the documented contract. */
describe('ActionPlanPreviewService — shared fixtures', () => {
  let svc: ActionPlanPreviewService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    svc = TestBed.inject(ActionPlanPreviewService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  it('posts the symmetric fixture verbatim and returns an empty warning list', async () => {
    const promise = svc.preview(paritySymmetric);

    const req = http.expectOne('/api/live-instances/preview-action-plan');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(paritySymmetric);
    const body: ActionPlanPreviewResponse = { warnings: [] };
    req.flush(body);

    await expect(promise).resolves.toEqual(body);
  });

  it('surfaces the orphan-entry warning shape from the preview endpoint', async () => {
    const promise = svc.preview(parityOrphan);

    const req = http.expectOne('/api/live-instances/preview-action-plan');
    expect(req.request.body).toEqual(parityOrphan);
    const body: ActionPlanPreviewResponse = {
      warnings: [
        {
          code: 'orphan_entry',
          message: "Entry leg 'spy_long' has no matching close_leg.",
          leg_id: 'spy_long',
        },
      ],
    };
    req.flush(body);

    const result = await promise;
    expect(result.warnings[0].code).toBe('orphan_entry');
    expect(result.warnings[0].leg_id).toBe('spy_long');
  });
});
