import { describe, expect, it } from 'vitest';

import { STATIC_ALPACA_BOT_CONTROL_FIXTURES } from '../components/examples/alpaca-bot-control/alpaca-bot-control-fixtures';

describe('Alpaca Clerk diagnostic fixture contract', () => {
  it('keeps all 15 Python-owned fixture envelopes consumable by Angular', () => {
    expect(STATIC_ALPACA_BOT_CONTROL_FIXTURES).toHaveLength(15);
    expect(new Set(STATIC_ALPACA_BOT_CONTROL_FIXTURES.map((fixture) => fixture.scenario_id))).toEqual(
      new Set([
        'running_ready_flat', 'running_attributed_long', 'entry_partial_pending',
        'exit_cancelling_entry', 'exit_closing', 'exit_complete', 'stopped_flat',
        'stop_requires_flatten', 'stopped_carryover_intact', 'stopped_carryover_mismatch',
        'instance_submit_uncertain', 'market_data_stale', 'account_unattributable',
        'account_unprovable', 'known_manual_exposure',
      ]),
    );
  });
});
