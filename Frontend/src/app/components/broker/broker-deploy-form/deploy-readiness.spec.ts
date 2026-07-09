import { describe, expect, it } from 'vitest';

import { actionPlanDeployReadiness } from './deploy-readiness';
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

describe('action-plan deploy readiness', () => {
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
});
