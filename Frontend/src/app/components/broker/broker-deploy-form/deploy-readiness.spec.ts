import { describe, expect, it } from 'vitest';

import {
  buildDeployChecks,
  buildDeployReadinessFacts,
  buildNowChecks,
} from './deploy-readiness';

describe('deploy readiness helpers', () => {
  it('maps stale engine code and account truth into named readiness facts', () => {
    const facts = buildDeployReadinessFacts({
      daemonState: 'ok',
      daemonFreshness: { state: 'stale', sha: 'abc1234', commitsBehind: 2 },
      brokerState: 'ok',
      brokerDetail: 'Connected',
      accountTruth: {
        final_verdict: 'not_proven',
        status_detail: 'Run account reconcile before starting.',
      },
      brokerAccountAvailable: true,
      fleetState: 'warn',
      nothingDeployed: false,
    });

    expect(facts).toEqual([
      expect.objectContaining({
        key: 'engine',
        condition: 'Stale code',
        state: 'warn',
        link: '/engine',
      }),
      expect.objectContaining({
        key: 'broker',
        condition: 'Linked',
        state: 'ok',
        link: '/broker/session-mirror',
      }),
      expect.objectContaining({
        key: 'account',
        condition: 'Not proven',
        detail: 'Run account reconcile before starting.',
        state: 'warn',
      }),
      expect.objectContaining({
        key: 'fleet',
        condition: 'Contaminated',
        state: 'warn',
        link: '/broker/reconciliation',
      }),
    ]);
  });

  it('preserves legacy now/deploy check semantics', () => {
    expect(
      buildNowChecks({
        daemonState: 'unknown',
        brokerState: 'down',
        fieldsReady: false,
        fleetState: 'unknown',
        nothingDeployed: true,
      }),
    ).toEqual([
      expect.objectContaining({ key: 'engine', detail: 'Checking' }),
      expect.objectContaining({ key: 'broker', detail: 'Disconnected' }),
      expect.objectContaining({ key: 'fields', detail: 'Required fields missing' }),
      expect.objectContaining({ key: 'fleet', detail: 'Nothing deployed' }),
    ]);

    expect(buildDeployChecks(409)).toEqual([
      expect.objectContaining({ key: 'tree', state: 'down' }),
      expect.objectContaining({ key: 'spec', state: 'pending' }),
    ]);
  });
});
