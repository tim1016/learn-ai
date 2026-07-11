// PRD #607 / Slice 1 (#608) — server <-> Frontend contract test.
//
// Snapshots captured from the running Python ``/api/live-instances/{id}/status``
// endpoint via ``PythonDataService/scripts/capture_operator_surface_fixture.py``.
// The committed JSON contains the route's ``operator_surface`` block only. A
// Python freshness test re-captures the route and compares that block to the
// committed JSON. This Vitest contract checks the same JSON payloads against the
// Frontend operator-surface type so shape drift (renamed field, dropped block,
// null/non-null mismatch) becomes a TypeScript build failure, NOT a silent
// runtime gap.
//
// To refresh: run the Python script after any projection change, then
// commit both the Python diff and the regenerated JSON fixtures in the same PR.

import { describe, expect, it } from 'vitest';

import steadyFixture from '../../testing/operator_surface_fixtures/steady.json';
import stoppedFixture from '../../testing/operator_surface_fixtures/stopped.json';
import type { OperatorSurface } from './live-instances.types';

// TypeScript widens JSON string literals to `string`, so raw JSON cannot
// satisfy closed string unions directly. The Python freshness test anchors
// literal values to backend output; this helper keeps the structural and
// nullability contract checked against the Frontend type.
type JsonImported<T> = T extends string
  ? string
  : T extends number
    ? number
    : T extends boolean
      ? boolean
      : T extends readonly (infer Item)[]
        ? JsonImported<Item>[]
        : T extends object
          ? { [Key in keyof T]: JsonImported<T[Key]> }
          : T;

const STEADY = steadyFixture satisfies JsonImported<OperatorSurface>;
const STOPPED = stoppedFixture satisfies JsonImported<OperatorSurface>;

describe('operator surface fixture wire contract', () => {
  it('STEADY fixture carries every projection block', () => {
    expect(STEADY.schema_version).toBe(1);
    expect(STEADY.host_process.state).toBe('RUNNING');
    expect(STEADY.host_process.notice).toBeNull();
    expect(STEADY.host_process.copyable_command).toBeNull();
    expect(STEADY.run_signal.state_label).toBe('On');
    expect(STEADY.run_signal.tone).toBe('on');
    expect(STEADY.actions.resume.enabled).toBe(false);
    expect(STEADY.actions.pause.enabled).toBe(true);
    expect(STEADY.actions.resume.disabled_reason_code).toBe(
      'POSTURE_DEMOTED',
    );
    expect(STEADY.actions.pause.disabled_reason_code).toBeNull();
    expect(STEADY.submit_readiness.code).toBe('broker_state_unproven');
    expect(STEADY.execution?.posture).toBe('UNKNOWN');
    expect(STEADY.trader_guidance.primary_remediation.kind).toBe(
      'open_runbook',
    );
    expect(STEADY.trader_guidance.primary_remediation).toMatchObject({
      slug: 'broker-instance-operator-surface',
    });
  });

  it('STOPPED fixture surfaces the host-process notice and reflects the unbound state', () => {
    // Daemon-`idle` + the test fixture's default desired_state=RUNNING
    // upgrades the host-process state to WAITING_FOR_HOST; absent desired
    // intent it stays IDLE.  The fixture captures the unbound (idle)
    // case.  PRD #616 left this enum unchanged.
    expect(STOPPED.host_process.state).toBe('IDLE');
    expect(STOPPED.host_process.notice).toMatch(/no active process/i);
    expect(STOPPED.run_signal.state_label).toBe('Off');
    expect(STOPPED.run_signal.tone).toBe('off');
    // flatten-and-pause requires a binding -> disabled with reason code.
    expect(STOPPED.actions.flatten_and_pause.enabled).toBe(false);
    expect(STOPPED.actions.flatten_and_pause.disabled_reason_code).toBe(
      'NO_LIVE_BINDING',
    );
    // PRD #616 / runtime-freshness hardening — resume is fail-closed when
    // broker safety/submission capability are not proven.
    expect(STOPPED.actions.resume.enabled).toBe(false);
    expect(STOPPED.actions.resume.disabled_reason_code).toBe(
      'BROKER_SAFETY_UNKNOWN',
    );
    expect(STOPPED.actions.pause.enabled).toBe(true);
  });

  it('exposes the expected top-level keys on every fixture', () => {
    // PRD #607 (cockpit revision) added ``trading_session``; PRD #616
    // added ``readiness_gates``.  Both fixtures must carry the full
    // set so the Bot Control renderer cannot encounter a missing block.
    const expected = [
      'schema_version',
      'host_process',
      'prior_run',
      'broker',
      'configuration',
      'current_risk',
      'daily_order_cap',
      'action_plan',
      'account_owner',
      'submit_readiness',
      'trader_guidance',
      'blockage_ladder',
      'run_signal',
      'actions',
      'confirmations',
      'trading_session',
      'readiness_gates',
      'blockers',
      'runtime_freshness',
      'control_plane',
      'broker_observation_consistency',
      'reconciliation',
      'broker_activity_health',
      'incident_headline',
      'notice_placement',
      'execution',
    ];
    for (const fixture of [STEADY, STOPPED]) {
      const actual = Object.keys(fixture).sort();
      expect(actual).toEqual([...expected].sort());
    }
  });

  it('every action capability carries the disabled_reasons list (PRD #616)', () => {
    for (const fixture of [STEADY, STOPPED]) {
      for (const action of Object.values(fixture.actions)) {
        expect(Array.isArray(action.disabled_reasons)).toBe(true);
      }
    }
  });

  it('exposes the five canonical actions including stop (PRD #616 / ADR-0010 §A1)', () => {
    const expected = new Set(['resume', 'pause', 'stop', 'flatten_and_pause', 'mark_poisoned']);
    for (const fixture of [STEADY, STOPPED]) {
      expect(new Set(Object.keys(fixture.actions))).toEqual(expected);
    }
  });
});
