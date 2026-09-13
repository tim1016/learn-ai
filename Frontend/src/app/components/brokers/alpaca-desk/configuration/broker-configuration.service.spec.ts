import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BrokerConfigurationService } from './broker-configuration.service';
import { resourceTarget } from '../../../../fleet/resource-target';

const CLERK = 'clrk_spec';
const PREFIX = `/api/brokers/alpaca/clerks/${CLERK}/configuration`;
const TARGET = resourceTarget('alpaca', CLERK, {
  capability: 'configuration_manage',
  idempotencyKey: 'configuration-spec-1',
  bindingGeneration: 7,
  routingEpoch: 4,
});

describe('BrokerConfigurationService', () => {
  let service: BrokerConfigurationService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
      provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerConfigurationService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('addresses every route relatively so the dev proxy attaches the control secret', () => {
    void service.readDeskState(CLERK);
    void service.readSelection(CLERK);
    void service.listCredentialSlots(CLERK);
    void service.listNicknames(CLERK);
    void service.listRevisions(CLERK, 'profile-1');

    const requests = http.match(() => true);
    expect(requests).toHaveLength(5);
    for (const request of requests) {
      // An absolute `environment.pythonServiceUrl` URL bypasses the proxy that
      // supplies `X-Data-Plane-Control-Secret`, and every route here — reads
      // included — is behind that header.
      expect(request.request.url.startsWith(PREFIX)).toBe(true);
      request.flush({});
    }
  });

  it('stages an exact revision under the generation it read', async () => {
    const staged = service.stageSelection(TARGET, 'profile-1', 4, 7);

    const request = http.expectOne(`${PREFIX}/selection`);
    expect(request.request.method).toBe('PUT');
    expect(request.request.body).toMatchObject({
      profile_id: 'profile-1',
      revision: 4,
      expected_selection_generation: 7,
    });
    expect((request.request.body as { command_context: { capability: string } }).command_context.capability).toBe('configuration_manage');
    request.flush({ selection_generation: 8 });

    await expect(staged).resolves.toMatchObject({ selection_generation: 8 });
  });

  it('applies against the generation it read, and sends nothing else', async () => {
    const applied = service.applySelection(TARGET, 9);

    const request = http.expectOne(`${PREFIX}/selection/apply`);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toMatchObject({ expected_selection_generation: 9 });
    request.flush({ selection_generation: 10, apply_requested: true });

    await expect(applied).resolves.toMatchObject({ apply_requested: true });
  });

  it('writes a revision against the revision the editor was opened on', () => {
    void service.createRevision(TARGET, 'profile-1', 3, {
      credential_slot: 'live',
      endpoint_mode: 'live',
      live_envelope: {
        loss_fraction: 0.05,
        loss_usd: 5000,
        shadow_sessions: 3,
        arming_max_sessions: 20,
        xh_entry_bps: 11,
        xh_exit_bps: 17.5,
      },
    });

    const request = http.expectOne(`${PREFIX}/profiles/profile-1/revisions`);
    expect(request.request.body).toMatchObject({
      expected_revision: 3,
      endpoint_mode: 'live',
      live_envelope: { shadow_sessions: 3 },
    });
    request.flush({});
  });

  it('verifies an account through the frozen lane command envelope', async () => {
    const verification = service.verifyAccount(TARGET, 'profile-1', 3);

    const request = http.expectOne(`${PREFIX}/profiles/profile-1/revisions/3/verify-account`);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      command_context: {
        capability: 'configuration_manage',
        idempotency_key: 'configuration-spec-1',
        expected_effective_binding_generation: 7,
      },
    });
    request.flush({ observed_accounts: [] });

    await expect(verification).resolves.toEqual([]);
  });

  it('escapes an identifier that would otherwise change the path it addresses', () => {
    void service.readProfile(CLERK, 'profile/../owner');
    void service.putNickname(TARGET, 'acct id/1', 'Testing');

    http.expectOne(`${PREFIX}/profiles/profile%2F..%2Fowner`).flush({});
    http.expectOne(`${PREFIX}/account-nicknames/acct%20id%2F1`).flush({});
  });

  it('asks for archived profiles only when the caller says so', () => {
    void service.listProfiles(CLERK);
    const hidden = http.expectOne((request) => request.url === `${PREFIX}/profiles`);
    expect(hidden.request.params.get('include_archived')).toBe('false');
    hidden.flush({ profiles: [] });

    void service.listProfiles(CLERK, { includeArchived: true });
    const shown = http.expectOne((request) => request.url === `${PREFIX}/profiles`);
    expect(shown.request.params.get('include_archived')).toBe('true');
    shown.flush({ profiles: [] });
  });

  it('never sends a credential field on any write', () => {
    void service.createProfile(TARGET, 'Paper — testing', {
      credential_slot: 'default',
      endpoint_mode: 'paper',
      live_envelope: null,
    });

    const request = http.expectOne(`${PREFIX}/profiles`);
    const body = JSON.stringify(request.request.body).toLowerCase();
    expect(body).not.toContain('credential_key');
    expect(body).not.toContain('credential_secret');
    request.flush({});
  });
});
