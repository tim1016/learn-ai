import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BrokerConfigurationService } from './broker-configuration.service';
import { resourceTarget } from '../../../../fleet/resource-target';
import catalogSnapshot from '../../../../fleet/fleet-operation-catalog.snapshot.json';

/**
 * Maps each public method of `broker-configuration.service.ts` to the
 * catalog operation id its body passes to `operationUrl`, read directly from
 * source. Two mocking approaches were tried and rejected first, both with
 * concrete evidence: `vi.spyOn(operationUrlModule, 'operationUrl')` returns
 * `undefined` rather than a spy, because a real ES module's named exports
 * are non-configurable, non-writable bindings that `Object.defineProperty`
 * cannot replace; and `vi.mock('../../../../fleet/operation-url', ...)`
 * fails the suite outright with "The 'vi.mock' and related methods are not
 * supported for relative imports with the Angular unit-test system." Static
 * extraction is the same technique `operation-url.spec.ts`'s "no service
 * reaches an unscoped broker path" test already uses for exactly this
 * reason (`readFileSync` + pattern match on the sibling service file).
 */
function operationIdsByMethod(): ReadonlyMap<string, string> {
  const source = readFileSync(join(__dirname, 'broker-configuration.service.ts'), 'utf8');
  // Every top-level (2-space-indented) method declaration starts a new
  // slice; the private `commandBody(target: ResourceTarget, ...)` helper
  // never matches (a space precedes its `(`, since it is declared as
  // `private commandBody(`), so only the 17 public HTTP-calling methods land
  // in the map.
  const methodStarts = [...source.matchAll(/\n {2}(\w+)\(/g)];
  const result = new Map<string, string>();
  methodStarts.forEach((match, index) => {
    const start = match.index ?? 0;
    const end = methodStarts[index + 1]?.index ?? source.length;
    const body = source.slice(start, end);
    const operationIdMatch = body.match(/operationUrl\('([a-z_]+)'/);
    if (operationIdMatch) result.set(match[1], operationIdMatch[1]);
  });
  return result;
}

const USED_OPERATION_IDS = operationIdsByMethod();
const CATALOG_OPERATIONS = catalogSnapshot.operations as Record<string, { method: string }>;

const CLERK = 'clrk_spec';
const BASE = `/api/brokers/alpaca/clerks/${CLERK}`;
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
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerConfigurationService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  /**
   * One row per HTTP call this service makes (#2109). Each row pins the
   * FULL resolved URL and method the service must produce — not merely
   * that `operationUrl` was called — because the failure mode this guards
   * against is a wrong-but-plausible URL reaching the coordinator as a
   * silent 404: several catalog operation ids differ only by verb on the
   * same path template (`configuration_profiles_list` GET /
   * `configuration_profile_create` POST on `/configuration/profiles`;
   * `configuration_selection_read` GET / `configuration_selection_write`
   * PUT on `/configuration/selection`), and others differ only in whether
   * a trailing path segment is present (`configuration_revisions_list` vs
   * `configuration_revision_read`, `configuration_verify_account` vs
   * `configuration_account_pin`). Getting one of the latter backwards
   * changes the URL and this table catches it directly; getting one of the
   * former backwards produces byte-identical output to the correct call
   * (see the note above the mutation ledger below) and is caught instead by
   * asserting `request.request.method`, which comes from which `this.http`
   * verb the call site invokes, independent of the operation id string.
   */
  const routes: readonly {
    name: string;
    method: string;
    url: string;
    invoke: () => unknown;
  }[] = [
    {
      name: 'readDeskState',
      method: 'GET',
      url: `${BASE}/configuration/desk-state`,
      invoke: () => service.readDeskState(CLERK),
    },
    {
      name: 'listCredentialSlots',
      method: 'GET',
      url: `${BASE}/configuration/credential-slots`,
      invoke: () => service.listCredentialSlots(CLERK),
    },
    {
      name: 'listProfiles',
      method: 'GET',
      // `listProfiles` always sends `include_archived` (default false), so
      // the exact-string matcher below — which Angular's
      // `HttpTestingController` compares against `urlWithParams`, not the
      // bare `url` — must include it too.
      url: `${BASE}/configuration/profiles?include_archived=false`,
      invoke: () => service.listProfiles(CLERK),
    },
    {
      name: 'readProfile',
      method: 'GET',
      url: `${BASE}/configuration/profiles/profile-1`,
      invoke: () => service.readProfile(CLERK, 'profile-1'),
    },
    {
      name: 'createProfile',
      method: 'POST',
      url: `${BASE}/configuration/profiles`,
      invoke: () =>
        service.createProfile(TARGET, 'Paper', {
          credential_slot: 'default',
          endpoint_mode: 'paper',
          live_envelope: null,
        }),
    },
    {
      name: 'updateProfile',
      method: 'PATCH',
      url: `${BASE}/configuration/profiles/profile-1`,
      invoke: () => service.updateProfile(TARGET, 'profile-1', { displayName: 'Renamed' }),
    },
    {
      name: 'cloneProfile',
      method: 'POST',
      url: `${BASE}/configuration/profiles/profile-1/clone`,
      invoke: () => service.cloneProfile(TARGET, 'profile-1', 'Clone'),
    },
    {
      name: 'listRevisions',
      method: 'GET',
      url: `${BASE}/configuration/profiles/profile-1/revisions`,
      invoke: () => service.listRevisions(CLERK, 'profile-1'),
    },
    {
      name: 'createRevision',
      method: 'POST',
      url: `${BASE}/configuration/profiles/profile-1/revisions`,
      invoke: () =>
        service.createRevision(TARGET, 'profile-1', 3, {
          credential_slot: 'default',
          endpoint_mode: 'paper',
          live_envelope: null,
        }),
    },
    {
      name: 'readRevision',
      method: 'GET',
      url: `${BASE}/configuration/profiles/profile-1/revisions/3`,
      invoke: () => service.readRevision(CLERK, 'profile-1', 3),
    },
    {
      name: 'verifyAccount',
      method: 'POST',
      url: `${BASE}/configuration/profiles/profile-1/revisions/3/verify-account`,
      invoke: () => service.verifyAccount(TARGET, 'profile-1', 3),
    },
    {
      name: 'pinAccount',
      method: 'POST',
      url: `${BASE}/configuration/profiles/profile-1/revisions/3/account-pin`,
      invoke: () => service.pinAccount(TARGET, 'profile-1', 3, 'PA9'),
    },
    {
      name: 'listNicknames',
      method: 'GET',
      url: `${BASE}/configuration/account-nicknames`,
      invoke: () => service.listNicknames(CLERK),
    },
    {
      name: 'putNickname',
      method: 'PUT',
      url: `${BASE}/configuration/account-nicknames/PA9`,
      invoke: () => service.putNickname(TARGET, 'PA9', 'Trading acct'),
    },
    {
      name: 'readSelection',
      method: 'GET',
      url: `${BASE}/configuration/selection`,
      invoke: () => service.readSelection(CLERK),
    },
    {
      name: 'stageSelection',
      method: 'PUT',
      url: `${BASE}/configuration/selection`,
      invoke: () => service.stageSelection(TARGET, 'profile-1', 3, 7),
    },
    {
      name: 'applySelection',
      method: 'POST',
      url: `${BASE}/configuration/selection/apply`,
      invoke: () => service.applySelection(TARGET, 7),
    },
  ];

  it.each(routes)('$name resolves to $method $url', ({ name, url, method, invoke }) => {
    void invoke();
    const request = http.expectOne(url);
    expect(request.request.method).toBe(method);

    // Catalog-anchored verb check. The URL/method pins above are blind to a
    // same-path verb-sibling mix-up (e.g. configuration_selection_read vs
    // configuration_selection_write, both `/configuration/selection`):
    // `operationUrl` encodes only the path, so swapping to the wrong sibling
    // while the call site still invokes the same `this.http.<verb>()`
    // produces a byte-identical request — the URL/method pins above cannot
    // see it. This closes that gap from the other side: read the operation
    // id this method's source actually names (via `USED_OPERATION_IDS`, not
    // a second hand-typed table) and require ITS catalog-declared method to
    // agree with the verb `this.http` really sent. A wrong-sibling swap
    // changes the named id's declared method without changing the sent
    // verb, so the two stop agreeing and this reddens.
    const usedOperationId = USED_OPERATION_IDS.get(name);
    expect(usedOperationId).toBeDefined();
    const declaredMethod = CATALOG_OPERATIONS[usedOperationId as string].method;
    expect(request.request.method.toUpperCase()).toBe(declaredMethod.toUpperCase());

    request.flush({});
  });

  it('covers all 17 HTTP call sites this service makes — a new one must be added here too', () => {
    expect(routes).toHaveLength(17);
  });

  it('stages an exact revision under the generation it read', async () => {
    const staged = service.stageSelection(TARGET, 'profile-1', 4, 7);

    const request = http.expectOne(`${BASE}/configuration/selection`);
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

    const request = http.expectOne(`${BASE}/configuration/selection/apply`);
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

    const request = http.expectOne(`${BASE}/configuration/profiles/profile-1/revisions`);
    expect(request.request.body).toMatchObject({
      expected_revision: 3,
      endpoint_mode: 'live',
      live_envelope: { shadow_sessions: 3 },
    });
    request.flush({});
  });

  it('verifies an account through the frozen lane command envelope', async () => {
    const verification = service.verifyAccount(TARGET, 'profile-1', 3);

    const request = http.expectOne(`${BASE}/configuration/profiles/profile-1/revisions/3/verify-account`);
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

  it('pins the observed account against the revision it was verified on', async () => {
    const pinned = service.pinAccount(TARGET, 'profile-1', 3, 'PA9');

    const request = http.expectOne(`${BASE}/configuration/profiles/profile-1/revisions/3/account-pin`);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toMatchObject({ account_id: 'PA9' });
    request.flush({ revision: 3 });

    await expect(pinned).resolves.toMatchObject({ revision: 3 });
  });

  it('escapes an identifier that would otherwise change the path it addresses', () => {
    void service.readProfile(CLERK, 'profile/../owner');
    void service.putNickname(TARGET, 'acct id/1', 'Testing');

    http.expectOne(`${BASE}/configuration/profiles/profile%2F..%2Fowner`).flush({});
    http.expectOne(`${BASE}/configuration/account-nicknames/acct%20id%2F1`).flush({});
  });

  it('asks for archived profiles only when the caller says so', () => {
    void service.listProfiles(CLERK);
    const hidden = http.expectOne((request) => request.url === `${BASE}/configuration/profiles`);
    expect(hidden.request.params.get('include_archived')).toBe('false');
    hidden.flush({ profiles: [] });

    void service.listProfiles(CLERK, { includeArchived: true });
    const shown = http.expectOne((request) => request.url === `${BASE}/configuration/profiles`);
    expect(shown.request.params.get('include_archived')).toBe('true');
    shown.flush({ profiles: [] });
  });

  it('never sends a credential field on any write', () => {
    void service.createProfile(TARGET, 'Paper — testing', {
      credential_slot: 'default',
      endpoint_mode: 'paper',
      live_envelope: null,
    });

    const request = http.expectOne(`${BASE}/configuration/profiles`);
    const body = JSON.stringify(request.request.body).toLowerCase();
    expect(body).not.toContain('credential_key');
    expect(body).not.toContain('credential_secret');
    request.flush({});
  });
});
