import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { SqliteSafeFlattenPlan } from '../../../../../api/alpaca.types';
import { SafeFlattenPlanComponent } from './safe-flatten-plan.component';

const PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token-17-exact',
  account_id: 'PA1',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  scope: 'BOT',
  strategy_instance_id: 'spy-bot',
  reconciliation_id: 'reconciliation-17',
  prepared_at_ms: 1_700_000_010_000,
  expires_at_ms: 1_700_000_039_000,
  legs: [{
    strategy_instance_id: 'spy-bot',
    symbol: 'SPY',
    side: 'sell',
    quantity: 1.25,
    position_updated_at_ms: 1_700_000_008_000,
  }],
};

describe('SafeFlattenPlanComponent', () => {
  it('renders the exact backend-authored plan without implying execution', async () => {
    await render(SafeFlattenPlanComponent, { inputs: { plan: PLAN } });

    expect(screen.getByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('Sell')).toBeTruthy();
    expect(screen.getByText('1.25')).toBeTruthy();
    expect(screen.getByText('spy-bot')).toBeTruthy();
    expect(screen.getByText('db-generation-4')).toBeTruthy();
    expect(screen.getByText('reconciliation-17')).toBeTruthy();
    expect(screen.getByText('plan-token-17-exact')).toBeTruthy();
    expect(screen.getByText(/No order has been submitted/)).toBeTruthy();
  });
});
