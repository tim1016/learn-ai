import { describe, expect, it } from 'vitest';

import {
  actionPlanDeployReadiness,
  buildDeployChecks,
  buildDeployReadinessFacts,
  buildNowChecks,
  deployBlocker,
  stoppedStartLatchState,
} from './deploy-readiness';
import snapshotJson from './action-plan-deploy-readiness.snapshot.json';
import { makeFrozenAccountTriage } from '../testing/account-triage-fixtures';

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
      accountTriage: null,
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

  it('lets active account freeze override clean account and fleet facts', () => {
    const facts = buildDeployReadinessFacts({
      daemonState: 'ok',
      daemonFreshness: { state: 'fresh', sha: 'abc1234', commitsBehind: null },
      brokerState: 'ok',
      brokerDetail: 'Connected',
      accountTruth: {
        final_verdict: 'clean',
        status_detail: 'Account Truth is clean.',
      },
      accountTriage: makeFrozenAccountTriage({
        accountId: 'DU123',
        conditionOptions: {
          conditionType: 'exposure_freeze',
          detail: 'watchdog.flatten_timed_out',
          operatorNextStep: 'CHECK_IBKR',
          source: 'watchdog_halt_executor',
          affectedStrategyInstanceIds: ['bot-a'],
          cureAction: 'resolve_exposure',
        },
      }),
      brokerAccountAvailable: true,
      fleetState: 'ok',
      nothingDeployed: false,
    });

    expect(facts).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'account', condition: 'Frozen', state: 'down' }),
        expect.objectContaining({ key: 'fleet', condition: 'Frozen', state: 'warn' }),
      ]),
    );
    expect(facts).not.toContainEqual(expect.objectContaining({ condition: 'Clean' }));
    expect(facts).not.toContainEqual(expect.objectContaining({ condition: 'Clear' }));
  });

  it('warns live-start now checks when account sick bay freezes the fleet', () => {
    const checks = buildNowChecks({
      daemonState: 'ok',
      brokerState: 'ok',
      fieldsReady: true,
      fleetState: 'ok',
      nothingDeployed: false,
      accountTriage: makeFrozenAccountTriage(),
    });

    expect(checks).toContainEqual(
      expect.objectContaining({
        key: 'fleet',
        state: 'warn',
        detail: 'Account frozen',
      }),
    );
  });

  it('blocks start-now deploys while account sick bay triage is loading', () => {
    expect(deployBlocker(baseDeployBlockerInput({ accountTriage: undefined }))).toEqual({
      message: 'Account sick bay is still loading. Wait for account readiness, or turn off "Start trading immediately" to deploy only.',
    });
  });

  it('blocks start-now deploys on active account freeze with the account monitor action', () => {
    expect(
      deployBlocker(baseDeployBlockerInput({ accountTriage: makeFrozenAccountTriage() })),
    ).toEqual({
      message: 'Account freeze active. Resolve the account sick-bay condition before starting.',
      actionLink: {
        message: 'Account freeze active.',
        route: '/broker/account-monitor',
        fragment: 'account-reconciliation-action',
        linkText: 'Open account monitor',
      },
    });
  });

  it('preserves legacy now/deploy check semantics', () => {
    expect(
      buildNowChecks({
        daemonState: 'unknown',
        brokerState: 'down',
        fieldsReady: false,
        fleetState: 'unknown',
        nothingDeployed: true,
        accountTriage: null,
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

function baseDeployBlockerInput(
  overrides: Partial<Parameters<typeof deployBlocker>[0]> = {},
): Parameters<typeof deployBlocker>[0] {
  return {
    daemonDown: false,
    effectiveStartNow: true,
    executionMode: 'paper_orders',
    allInCoexistenceBlock: null,
    fleetBlocksStarts: false,
    instanceAlreadyRunning: false,
    instanceId: 'deployment-validation-paper',
    brokerAccountAvailable: true,
    accountTruth: null,
    accountTriage: null,
    strategyKey: 'deployment_validation',
    strategySelected: true,
    required: true,
    missingRequiredFields: [],
    identityConflictSummary: null,
    exposureConflictSummary: null,
    actionPlanReadiness: {
      canDeploy: true,
      reasonCode: null,
      message: 'Action plan is ready for deployment.',
    },
    customSizingError: null,
    stoppedStartLatchState: 'clear',
    ...overrides,
  };
}
