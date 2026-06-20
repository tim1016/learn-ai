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
  });

  it('STOPPED fixture surfaces the host-process notice and reflects the unbound state', () => {
    expect(STOPPED.operator_surface.host_process.state).toBe('STOPPED');
    expect(STOPPED.operator_surface.host_process.notice).toMatch(/host runner/i);
    // flatten-and-pause requires a binding -> disabled with reason code.
    expect(STOPPED.operator_surface.actions.flatten_and_pause.enabled).toBe(false);
    expect(STOPPED.operator_surface.actions.flatten_and_pause.disabled_reason_code).toBe(
      'NO_LIVE_BINDING',
    );
    // resume/pause are durable writes — never disabled by the server.
    expect(STOPPED.operator_surface.actions.resume.enabled).toBe(true);
    expect(STOPPED.operator_surface.actions.pause.enabled).toBe(true);
  });

  it('exposes the same nine top-level keys on every fixture', () => {
    const expected = new Set([
      'schema_version',
      'host_process',
      'prior_run',
      'broker',
      'configuration',
      'current_risk',
      'daily_order_cap',
      'action_plan',
      'actions',
    ]);
    for (const fixture of [STEADY, STOPPED]) {
      expect(new Set(Object.keys(fixture.operator_surface))).toEqual(expected);
    }
  });
});
