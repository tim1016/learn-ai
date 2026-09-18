import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { resourceTarget } from '../../../../fleet/resource-target';
import { LiveGraduationService, type LiveGraduationPlan } from './live-graduation.service';

const CLERK = 'clrk_live';
const ACCOUNT = '318420190';
const BASE = `/api/brokers/alpaca/clerks/${CLERK}/accounts/${ACCOUNT}/live-graduation`;
const TARGET = resourceTarget('alpaca', CLERK, {
  accountId: ACCOUNT,
  capability: 'custody_command',
  idempotencyKey: 'graduation-1',
  bindingGeneration: 4,
  routingEpoch: 7,
});
const PLAN = {
  plan_id: 'a'.repeat(64),
  confirmation_token: 'b'.repeat(64),
} as LiveGraduationPlan;

describe('LiveGraduationService', () => {
  let service: LiveGraduationService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(LiveGraduationService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('reads status from the exact account-scoped lane operation', () => {
    void service.readStatus(CLERK, ACCOUNT);

    const request = http.expectOne(BASE);
    expect(request.request.method).toBe('GET');
    request.flush({});
  });

  it('prepares through a custody command envelope with no caller-authored evidence', () => {
    void service.prepare(TARGET);

    const request = http.expectOne(`${BASE}/plan`);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      command_context: {
        capability: 'custody_command',
        idempotency_key: 'graduation-1',
        expected_effective_binding_generation: 4,
        target: { account_id: ACCOUNT },
      },
    });
    request.flush({});
  });

  it('applies only the content-addressed plan and confirmation token', () => {
    void service.apply(TARGET, PLAN);

    const request = http.expectOne(`${BASE}/apply`);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      plan_id: 'a'.repeat(64),
      confirmation_token: 'b'.repeat(64),
      command_context: {
        capability: 'custody_command',
        idempotency_key: 'graduation-1',
        expected_effective_binding_generation: 4,
        target: { account_id: ACCOUNT },
      },
    });
    expect(request.request.body).not.toHaveProperty('force');
    expect(request.request.body).not.toHaveProperty('artifacts_root');
    expect(request.request.body).not.toHaveProperty('broker_evidence');
    request.flush({});
  });
});

