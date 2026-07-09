import { describe, expect, it } from 'vitest';

import type {
  BotDailyLifecycleProjection,
  LiveInstanceStatus,
  OperatorSurfaceCurrentRisk,
} from '../../../../api/live-instances.types';
import { makeStatus } from '../bot-control-page.fixtures';
import { makeDailyLifecycleFixture } from '../../../../testing/live-instance-status-fixtures';
import { formatPosition, resolveVerdictCardModel } from './verdict-card-model';

function statusWith(
  lifecycle: Partial<BotDailyLifecycleProjection>,
  mutate?: (status: LiveInstanceStatus) => void,
): LiveInstanceStatus {
  const status = makeStatus();
  status.daily_lifecycle = makeDailyLifecycleFixture(lifecycle);
  mutate?.(status);
  return status;
}

function addRetiredTerminalBlocker(status: LiveInstanceStatus): void {
  status.operator_surface.blockers = [
    {
      id: 'retired',
      severity: 'blocking',
      disposition: 'terminal',
      headline: "Can't recover",
      detail: 'This bot has been retired. Remove it from the catalog or replace it.',
      primary_move: {
        label: 'Remove',
        action: { kind: 'remove' },
        target: null,
      },
      secondary_moves: [
        {
          label: 'Replace',
          action: { kind: 'retire_replace' },
          target: null,
        },
      ],
      applies_to: 'run',
    },
  ];
}

const RISK: OperatorSurfaceCurrentRisk = {
  posture: 'LONG',
  owned_positions: { SPY: 40 },
  pending_order_count: 0,
  verdict: 'READY',
  unrealized_pnl: 127.4,
};

describe('resolveVerdictCardModel', () => {
  it('maps a Ready bot to a full positive card with the lifecycle start verb', () => {
    const model = resolveVerdictCardModel(statusWith({ display_status: 'Ready' }));

    expect(model.state).toBe('Ready');
    expect(model.tone).toBe('positive');
    expect(model.layout).toBe('full');
    expect(model.verb).toEqual({
      kind: 'lifecycle',
      action: expect.objectContaining({ id: 'confirm_start' }),
    });
    expect(model.showChart).toBe(false);
  });

  it('maps an On duty bot to a strip layout with vitals and the chart', () => {
    const model = resolveVerdictCardModel(
      statusWith(
        {
          display_status: 'On duty',
          phase: 'ON_DUTY',
          primary_action: {
            id: 'end_day_now',
            label: 'End day now',
            enabled: true,
            reason: null,
            offer_id: null,
            expires_at_ms: null,
          },
        },
        (status) => {
          status.operator_surface.current_risk = RISK;
        },
      ),
    );

    expect(model.layout).toBe('strip');
    expect(model.showChart).toBe(true);
    expect(model.verb).toEqual({
      kind: 'lifecycle',
      action: expect.objectContaining({ id: 'end_day_now' }),
    });
    expect(model.vitals.map((vital) => vital.label)).toEqual([
      'Position',
      'Unrealized P&L',
      'Orders today',
    ]);
  });

  it('falls back to the trader remediation verb for a Sick bay bot with no lifecycle action', () => {
    const model = resolveVerdictCardModel(
      statusWith({ display_status: 'Sick bay', primary_action: null }),
    );

    expect(model.tone).toBe('danger');
    expect(model.verb).toEqual({ kind: 'remediation' });
    expect(model.vitals.map((vital) => vital.label)).toEqual([
      'Position',
      'Unrealized P&L',
      'Session',
    ]);
  });

  it('routes a self-targeting open_runbook remediation to the evidence drawer', () => {
    // watchdog-halt / runtime-freshness resolve to this same bot page, so
    // navigating is a no-op; the verb must open the why-drawer instead.
    const model = resolveVerdictCardModel(
      statusWith({ display_status: 'Sick bay', primary_action: null }, (status) => {
        status.operator_surface.trader_guidance.primary_remediation = {
          kind: 'open_runbook',
          slug: 'watchdog-halt',
        };
      }),
    );

    expect(model.verb).toEqual({ kind: 'evidence' });
  });

  it('still navigates for an off-page open_runbook remediation', () => {
    const model = resolveVerdictCardModel(
      statusWith({ display_status: 'Sick bay', primary_action: null }, (status) => {
        status.operator_surface.trader_guidance.primary_remediation = {
          kind: 'open_runbook',
          slug: 'broker-reconnect',
        };
      }),
    );

    expect(model.verb).toEqual({ kind: 'remediation' });
  });

  it('shows no verb but a checklist while Clocking out', () => {
    const model = resolveVerdictCardModel(statusWith({ display_status: 'Clocking out' }));

    expect(model.verb).toEqual({ kind: 'none' });
    expect(model.showChecklist).toBe(true);
  });

  it('treats a crash-recovery start gate as the top-priority verb', () => {
    const model = resolveVerdictCardModel(
      statusWith({ display_status: 'Sick bay', primary_action: null }, (status) => {
        status.operator_surface.host_process.start_capability = {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'CRASH_RECOVERY_REQUIRED',
          gate_results: [],
        };
      }),
    );

    expect(model.verb).toEqual({ kind: 'crash_recovery' });
  });

  it('renders a retired terminal blocker as the dead-end with only terminal moves', () => {
    // The default status carries a reconcile remediation; a retired bot must
    // still show no hopeful verb because the backend authored terminal moves.
    const model = resolveVerdictCardModel(
      statusWith(
        { display_status: 'Retired', phase: 'RETIRED', primary_action: null },
        addRetiredTerminalBlocker,
      ),
    );

    expect(model.readOnly).toBe(true);
    expect(model.stateLabel).toBe("Can't recover");
    expect(model.tone).toBe('danger');
    expect(model.verb).toEqual({ kind: 'none' });
    expect(model.terminalMoves.map((move) => move.action.kind)).toEqual([
      'remove',
      'retire_replace',
    ]);
  });

  it('suppresses lifecycle overflow for a poisoned non-retired terminal blocker', () => {
    const model = resolveVerdictCardModel(
      statusWith({ display_status: 'Off duty', phase: 'OFF_DUTY' }, (status) => {
        status.operator_surface.blockers = [
          {
            id: 'run_poisoned',
            severity: 'blocking',
            disposition: 'terminal',
            headline: "Can't recover",
            detail: 'This run is poisoned and cannot be restarted safely.',
            primary_move: {
              label: 'Replace',
              action: { kind: 'retire_replace' },
              target: null,
            },
            secondary_moves: [
              {
                label: 'Remove',
                action: { kind: 'remove' },
                target: null,
              },
            ],
            applies_to: 'run',
          },
        ];
      }),
    );

    expect(model.stateLabel).toBe("Can't recover");
    expect(model.verb).toEqual({ kind: 'none' });
    expect(model.ambientActions).toEqual([]);
    expect(model.showOverflow).toBe(false);
    expect(model.terminalMoves.map((move) => move.action.kind)).toEqual([
      'retire_replace',
      'remove',
    ]);
  });
});

describe('formatPosition', () => {
  it('summarizes a single-symbol long position', () => {
    expect(formatPosition(RISK)).toBe('Long 40 SPY');
  });

  it('renders flat and unproven honestly', () => {
    expect(formatPosition({ ...RISK, posture: 'FLAT' })).toBe('Flat');
    expect(formatPosition({ ...RISK, posture: 'UNKNOWN' })).toBe('Not proven');
  });
});
