import { describe, expect, it } from 'vitest';

import {
  actionPlanDeployReadiness,
  buildDeployChecks,
  buildDeployReadinessFacts,
  buildNowChecks,
  stoppedStartLatchState,
} from './deploy-readiness';
import snapshotJson from './action-plan-deploy-readiness.snapshot.json';

interface ActionPlanDeployReadinessSnapshot {
  readonly $comment: string;
  readonly generated_by: string;
  readonly source_files: readonly string[];
  readonly cases: readonly {
    readonly id: string;
    readonly strategy_key: string;
    readonly action_plan: unknown;
    readonly can_deploy: boolean;
    readonly reason_code: string | null;
    readonly message: string;
  }[];
}

const actionPlanSnapshot = snapshotJson satisfies ActionPlanDeployReadinessSnapshot;

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

  it.each(actionPlanSnapshot.cases)(
    'matches backend action-plan deploy readiness snapshot: $id',
    (scenario) => {
      expect(actionPlanDeployReadiness(scenario.strategy_key, scenario.action_plan)).toEqual({
        canDeploy: scenario.can_deploy,
        reasonCode: scenario.reason_code,
        message: scenario.message,
      });
    },
  );

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
	      statusLoading: false,
	      statusUnavailable: false,
	      desiredState: null,
	      startCapability: null,
	    };

	    expect(stoppedStartLatchState(base)).toBe('clear');
	    expect(stoppedStartLatchState({ ...base, statusLoading: true })).toBe('checking');
	    expect(stoppedStartLatchState({ ...base, statusUnavailable: true })).toBe('unknown');
	    expect(stoppedStartLatchState({ ...base, startNow: false })).toBe('not_applicable');
	    expect(stoppedStartLatchState({ ...base, instanceId: '' })).toBe('not_applicable');
	    expect(stoppedStartLatchState({ ...base, instanceIdValid: false })).toBe('not_applicable');
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
