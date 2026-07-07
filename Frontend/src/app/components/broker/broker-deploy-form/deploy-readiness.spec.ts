import { describe, expect, it } from 'vitest';

import {
  actionPlanDeployReadiness,
  buildDeployChecks,
  buildDeployReadinessFacts,
  buildNowChecks,
} from './deploy-readiness';

describe('deploy readiness helpers', () => {
  it('blocks deployment-validation while required action-plan legs are missing', () => {
    expect(
      actionPlanDeployReadiness('deployment_validation', { on_enter: [], on_exit: [] }),
    ).toEqual(
      expect.objectContaining({
        canDeploy: false,
        reasonCode: 'ACTION_PLAN_EMPTY',
      }),
    );
    expect(
      actionPlanDeployReadiness('deployment_validation', {
        on_enter: [],
        on_exit: [{ kind: 'close_leg', entry_leg_id: 'spy_long' }],
      }),
    ).toEqual(
      expect.objectContaining({
        canDeploy: false,
        reasonCode: 'ACTION_PLAN_ENTRY_LEG_REQUIRED',
      }),
    );
  });

  it('blocks deployment-validation action-plan shapes the runtime cannot consume', () => {
    expect(
      actionPlanDeployReadiness('deployment_validation', {
        on_enter: [
          {
            leg_id: 'spy_short',
            instrument: { kind: 'stock', underlying: 'SPY' },
            position: 'short',
            qty_ratio: 1,
          },
        ],
        on_exit: [{ kind: 'close_leg', entry_leg_id: 'spy_short' }],
      }),
    ).toEqual(
      expect.objectContaining({
        canDeploy: false,
        reasonCode: 'ACTION_PLAN_UNSUPPORTED',
      }),
    );

    expect(
      actionPlanDeployReadiness('deployment_validation', {
        on_enter: [
          {
            leg_id: 'spy_long',
            instrument: { kind: 'stock', underlying: 'SPY' },
            position: 'long',
            qty_ratio: 1,
          },
        ],
        on_exit: [],
      }),
    ).toEqual(
      expect.objectContaining({
        canDeploy: false,
        reasonCode: 'ACTION_PLAN_CLOSE_LEG_REQUIRED',
      }),
    );
  });

  it('accepts deployment-validation stock plans without overblocking other strategies', () => {
    const entryOnlyStockPlan = {
      on_enter: [
        {
          leg_id: 'spy_long',
          instrument: { kind: 'stock' as const, underlying: 'SPY' },
          position: 'long' as const,
          qty_ratio: 1,
        },
      ],
      on_exit: [],
    };

    expect(actionPlanDeployReadiness('spy_ema_crossover', entryOnlyStockPlan)).toEqual(
      expect.objectContaining({ canDeploy: true }),
    );
    expect(
      actionPlanDeployReadiness('deployment_validation', {
        ...entryOnlyStockPlan,
        on_exit: [{ kind: 'close_leg', entry_leg_id: 'spy_long' }],
      }),
    ).toEqual(expect.objectContaining({ canDeploy: true }));
  });

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
