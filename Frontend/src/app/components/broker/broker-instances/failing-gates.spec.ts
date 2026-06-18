import { describe, expect, it } from 'vitest';
import type { ReadinessGate, ReadinessVector } from '../../../api/live-instances.types';

import { projectFailingGates } from './failing-gates';

function makeGate(overrides: Partial<ReadinessGate> = {}): ReadinessGate {
  return {
    name: 'desired_state',
    status: 'fail',
    severity: 'hard',
    detail: 'No intent set',
    ...overrides,
  };
}

function makeReadiness(gates: ReadinessGate[]): ReadinessVector {
  return {
    kind: 'live_readiness',
    as_of_ms: 0,
    source: 'engine',
    verdict: 'BLOCKED',
    summary: '',
    gates,
  };
}

const LABELS: Record<string, string> = {
  desired_state: 'Bot Intent Set',
  orders_cap: 'Daily Trade Limit Available',
};

describe('projectFailingGates', () => {
  it('returns [] when readiness is null', () => {
    expect(projectFailingGates(null, LABELS)).toEqual([]);
  });

  it('omits passing gates', () => {
    const r = makeReadiness([
      makeGate({ name: 'desired_state', status: 'pass' }),
      makeGate({ name: 'orders_cap', status: 'fail' }),
    ]);

    expect(projectFailingGates(r, LABELS).map((g) => g.key)).toEqual(['orders_cap']);
  });

  it('maps each gate.name through the labels map', () => {
    const r = makeReadiness([makeGate({ name: 'desired_state' })]);

    expect(projectFailingGates(r, LABELS)[0]?.label).toBe('Bot Intent Set');
  });

  it('falls back to the raw gate.name when the labels map has no entry', () => {
    const r = makeReadiness([makeGate({ name: 'unmapped_gate' })]);

    expect(projectFailingGates(r, LABELS)[0]?.label).toBe('unmapped_gate');
  });

  it('carries severity and detail through unchanged', () => {
    const r = makeReadiness([
      makeGate({ name: 'desired_state', severity: 'soft', detail: 'soft fail detail' }),
    ]);

    const row = projectFailingGates(r, LABELS)[0];
    expect(row?.severity).toBe('soft');
    expect(row?.detail).toBe('soft fail detail');
  });
});
