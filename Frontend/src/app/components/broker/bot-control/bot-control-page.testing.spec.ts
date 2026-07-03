import { describe, expect, it } from 'vitest';

import { makeFailClosedLiveRuns } from './bot-control-page.testing';

describe('Bot Control page testing harness', () => {
  it('rejects unconfigured mutations with the fail-closed harness error', async () => {
    const liveRuns = makeFailClosedLiveRuns();
    const startRequest = {
      readonly: false,
      hydrate_policy: 'require',
      strategy: 'deployment_validation',
      max_orders_per_day: 2,
      ibkr_host: '127.0.0.1',
    } as const;
    const mutationCalls: { name: string; call: () => Promise<unknown> }[] = [
      {
        name: 'renewControlPlaneLease',
        call: () => liveRuns.renewControlPlaneLease(),
      },
      {
        name: 'startHostRunner',
        call: () => liveRuns.startHostRunner('run-x', startRequest),
      },
      {
        name: 'setInstanceDesiredState',
        call: () => liveRuns.setInstanceDesiredState('sid-x', {
          action: 'pause',
          reason: 'Pause',
          updated_by: 'operator',
        }),
      },
      {
        name: 'flattenAndPause',
        call: () => liveRuns.flattenAndPause('sid-x', {
          action: 'pause',
          reason: 'Flatten and pause',
          updated_by: 'operator',
        }),
      },
      {
        name: 'issueInstanceCommand',
        call: () => liveRuns.issueInstanceCommand('sid-x', { verb: 'MARK_POISONED' }),
      },
      {
        name: 'reconcileInstance',
        call: () => liveRuns.reconcileInstance('sid-x'),
      },
    ];

    for (const { name, call } of mutationCalls) {
      await expect(call()).rejects.toThrow(
        `${name} was invoked without an explicit Bot Control harness mutation override.`,
      );
    }
  });
});
