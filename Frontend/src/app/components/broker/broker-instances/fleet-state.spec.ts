import { describe, expect, it } from 'vitest';
import type { LiveInstanceStatus, ReadinessVerdict } from '../../../api/live-instances.types';

import { deriveFleetState } from './fleet-state';

function makeStatus(verdict: ReadinessVerdict | undefined): LiveInstanceStatus {
  return {
    readiness: verdict
      ? {
          kind: 'live_readiness',
          as_of_ms: 0,
          source: 'engine',
          verdict,
          summary: '',
          gates: [],
        }
      : null,
  } as unknown as LiveInstanceStatus;
}

describe('deriveFleetState', () => {
  it('returns STEADY when readiness verdict is READY', () => {
    expect(deriveFleetState(makeStatus('READY'))).toBe('STEADY');
  });

  it('returns CONFIGURE when readiness verdict is DEGRADED', () => {
    expect(deriveFleetState(makeStatus('DEGRADED'))).toBe('CONFIGURE');
  });

  it('returns BLOCKED when readiness verdict is BLOCKED', () => {
    expect(deriveFleetState(makeStatus('BLOCKED'))).toBe('BLOCKED');
  });

  it('returns BLOCKED when readiness verdict is UNKNOWN', () => {
    expect(deriveFleetState(makeStatus('UNKNOWN'))).toBe('BLOCKED');
  });

  it('returns BLOCKED when there is no readiness vector', () => {
    expect(deriveFleetState(makeStatus(undefined))).toBe('BLOCKED');
  });
});
