import { describe, expect, it } from 'vitest';

import { STATIC_ALPACA_BOT_CONTROL_FIXTURES } from '../components/examples/alpaca-bot-control/alpaca-bot-control-fixtures';

describe('Alpaca Clerk diagnostic fixture contract', () => {
  it('keeps all 15 Python-owned fixture envelopes consumable by Angular', () => {
    expect(STATIC_ALPACA_BOT_CONTROL_FIXTURES).toHaveLength(15);
    const scenarioIds = STATIC_ALPACA_BOT_CONTROL_FIXTURES.map(
      (fixture) => fixture.scenario_id,
    );
    expect(new Set(scenarioIds).size).toBe(scenarioIds.length);
  });
});
