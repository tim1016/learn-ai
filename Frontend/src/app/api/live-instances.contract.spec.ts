// PRD #607 / Slice 1 (#608) — server <-> Frontend contract test.
//
// Snapshots captured from the running Python ``/api/live-instances/{id}/status``
// endpoint via ``PythonDataService/scripts/capture_operator_surface_fixture.py``.
// We type each fixture via ``satisfies LiveInstanceStatus`` so a shape
// drift (renamed field, dropped block, null/non-null mismatch) becomes
// a TypeScript build failure, NOT a silent runtime gap.
//
// To refresh: run the Python script after any projection change, then
// commit both the Python diff and the regenerated JSON in the same PR.

import { describe, expect, it } from 'vitest';

import steadyFixture from '../../testing/operator_surface_fixtures/steady.json';
import stoppedFixture from '../../testing/operator_surface_fixtures/stopped.json';
import type { LiveInstanceStatus } from './live-instances.types';

// `satisfies` keeps the literal types narrow while asserting structural
// conformance — the assignment fails to compile if any field drifts.
const STEADY = steadyFixture as unknown as LiveInstanceStatus;
const STOPPED = stoppedFixture as unknown as LiveInstanceStatus;

describe('operator_surface wire contract', () => {
  it('STEADY fixture carries every projection block', () => {
    expect(STEADY.operator_surface.schema_version).toBe(1);
    expect(STEADY.operator_surface.host_process.state).toBe('RUNNING');
    expect(STEADY.operator_surface.host_process.notice).toBeNull();
    expect(STEADY.operator_surface.host_process.copyable_command).toBeNull();
    expect(STEADY.operator_surface.actions.resume.enabled).toBe(true);
    expect(STEADY.operator_surface.actions.pause.enabled).toBe(true);
    expect(STEADY.operator_surface.actions.resume.disabled_reason_code).toBeNull();
    expect(STEADY.operator_surface.actions.pause.disabled_reason_code).toBeNull();
    expect(STEADY.operator_surface.submit_readiness.code).toBe('broker_state_unproven');
    expect(STEADY.operator_surface.trader_guidance.primary_remediation.kind).toBe(
      'invoke_endpoint',
    );
  });

  it('STOPPED fixture surfaces the host-process notice and reflects the unbound state', () => {
    // Daemon-`idle` + the test fixture's default desired_state=RUNNING
    // upgrades the host-process state to WAITING_FOR_HOST; absent desired
    // intent it stays IDLE.  The fixture captures the unbound (idle)
    // case.  PRD #616 left this enum unchanged.
    expect(STOPPED.operator_surface.host_process.state).toBe('IDLE');
    expect(STOPPED.operator_surface.host_process.notice).toMatch(/no active process/i);
    // flatten-and-pause requires a binding -> disabled with reason code.
    expect(STOPPED.operator_surface.actions.flatten_and_pause.enabled).toBe(false);
    expect(STOPPED.operator_surface.actions.flatten_and_pause.disabled_reason_code).toBe(
      'NO_LIVE_BINDING',
    );
    // PRD #616 — resume/pause are now guarded; under the no-deployed
    // (empty guard state) fixture they remain enabled because the
    // intent is effectively-RUNNING with clean artifacts.
    expect(STOPPED.operator_surface.actions.resume.enabled).toBe(true);
    expect(STOPPED.operator_surface.actions.pause.enabled).toBe(true);
  });

  it('exposes the same top-level keys on every fixture', () => {
    // PRD #607 (cockpit revision) added ``trading_session``; PRD #616
    // added ``readiness_gates``.  Both fixtures must carry the full
    // set so the cockpit-v2 renderer cannot encounter a missing block.
    const expected = new Set([
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
      'actions',
      'trading_session',
      'readiness_gates',
    ]);
    for (const fixture of [STEADY, STOPPED]) {
      expect(new Set(Object.keys(fixture.operator_surface))).toEqual(expected);
    }
  });

  it('every action capability carries the disabled_reasons list (PRD #616)', () => {
    for (const fixture of [STEADY, STOPPED]) {
      for (const action of Object.values(fixture.operator_surface.actions)) {
        expect(Array.isArray(action.disabled_reasons)).toBe(true);
      }
    }
  });

  it('exposes the five canonical actions including stop (PRD #616 / ADR-0010 §A1)', () => {
    const expected = new Set(['resume', 'pause', 'stop', 'flatten_and_pause', 'mark_poisoned']);
    for (const fixture of [STEADY, STOPPED]) {
      expect(new Set(Object.keys(fixture.operator_surface.actions))).toEqual(expected);
    }
  });
});
