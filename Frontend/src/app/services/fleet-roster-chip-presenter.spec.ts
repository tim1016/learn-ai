import { describe, expect, it } from 'vitest';

import { presentFleetRosterChips } from './fleet-roster-chip-presenter';

describe('fleet roster chip presenter', () => {
  it('returns interim blocker chips for non-ready roster rows only', () => {
    expect(
      presentFleetRosterChips([
        {
          strategy_instance_id: 'ready-bot',
          process_state: 'running',
          readiness_verdict: 'READY',
        },
        {
          strategy_instance_id: 'blocked-bot',
          process_state: 'idle',
          readiness_verdict: 'BLOCKED',
        },
        {
          strategy_instance_id: 'unknown-bot',
          process_state: 'unreachable',
        },
      ]),
    ).toEqual([
      {
        id: 'blocked-bot',
        label: 'blocked-bot',
        processState: 'idle',
        readinessVerdict: 'BLOCKED',
        state: 'warn',
      },
      {
        id: 'unknown-bot',
        label: 'unknown-bot',
        processState: 'unreachable',
        readinessVerdict: 'UNKNOWN',
        state: 'down',
      },
    ]);
  });
});
