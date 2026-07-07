import { describe, expect, it } from 'vitest';

import {
  buildDeployChecks,
  buildDeployReadinessFacts,
  buildNowChecks,
  stoppedStartLatchState,
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

  it('detects durable STOPPED from desired state, start capability, or gate next-step', () => {
    const base = {
      startNow: true,
      instanceId: 'bot-1',
      instanceIdValid: true,
      statusRequired: true,
      statusLoading: false,
      desiredState: null,
      startCapability: null,
    };

    expect(stoppedStartLatchState(base)).toBe('clear');
    expect(stoppedStartLatchState({ ...base, statusLoading: true })).toBe('checking');
    expect(stoppedStartLatchState({ ...base, startNow: false })).toBe('not_applicable');
    expect(stoppedStartLatchState({ ...base, instanceId: '' })).toBe('not_applicable');
    expect(stoppedStartLatchState({ ...base, instanceIdValid: false })).toBe('not_applicable');
    expect(
      stoppedStartLatchState({
        ...base,
        statusRequired: false,
        desiredState: {
          state: 'STOPPED',
          updated_at_ms: 1,
          updated_by: 'operator',
          reason: null,
          version: 1,
          path_status: 'ok',
        },
      }),
    ).toBe('clear');
    expect(
      stoppedStartLatchState({
        ...base,
        desiredState: {
          state: 'STOPPED',
          updated_at_ms: 1,
          updated_by: 'operator',
          reason: null,
          version: 1,
          path_status: 'ok',
        },
      }),
    ).toBe('blocked');
    expect(
      stoppedStartLatchState({
        ...base,
        startCapability: {
          disabled_reason_code: 'STOPPED_REQUIRES_RESUME',
          gate_results: [],
        },
      }),
    ).toBe('blocked');
    expect(
      stoppedStartLatchState({
        ...base,
        startCapability: {
          disabled_reason_code: null,
          gate_results: [
            {
              gate_id: 'desired_state.start',
              status: 'block',
              source: 'desired_state',
              operator_reason: 'STOPPED',
              operator_next_step: 'STOPPED_REQUIRES_RESUME',
              evidence_at_ms: 1,
            },
          ],
        },
      }),
    ).toBe('blocked');
  });
});
