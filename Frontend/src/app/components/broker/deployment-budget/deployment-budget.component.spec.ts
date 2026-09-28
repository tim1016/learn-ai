import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import { resourceTarget } from '../../../fleet/resource-target';
import { BrokerV2PanelService, type DeploymentBudgetView } from '../v2-panel/lib/broker-v2-panel.service';
import { DeploymentBudgetComponent } from './deployment-budget.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const VIEW: DeploymentBudgetView = {
  state: 'ready', detail: 'Stopped. Cancellation is still pending.', strategy_instance_id: 'stopped-a', world: 'real_paper',
  committed_usd: '1000.00', realized_gross_usd: '-10.00', fees_usd: '0.03', position_cost_usd: '0.00',
  pending_orders_usd: '600.00', free_usd: '0.00', released_usd: '389.97', outstanding_cash_usd: '600.00',
  shortfall_usd: '0.00', entry_eligible: false, observed_at_ms: 1_800_000_000_000,
};

describe('deployment money evidence', () => {
  it('keeps released cash separate from cash still claimed after Stop', async () => {
    await render(DeploymentBudgetComponent, { inputs: { target: TARGET, strategyInstanceId: 'stopped-a' },
      providers: [{ provide: BrokerV2PanelService, useValue: { getBudget: vi.fn().mockResolvedValue(VIEW) } }] });
    await screen.findByText('Stopped. Cancellation is still pending.');
    expect(screen.getByText('Cash released').nextElementSibling?.textContent).toBe('$389.97');
    expect(screen.getByText('Cash still claimed by orders, fills or fees').nextElementSibling?.textContent).toBe('$600.00');
    expect(screen.getByText(/New entries unavailable/)).toBeTruthy();
  });

  it('shows unknown amounts and refreshes for a new custody revision', async () => {
    const getBudget = vi.fn().mockResolvedValue({ ...VIEW, state: 'unavailable', detail: 'Fee evidence is incomplete.', free_usd: null });
    const { fixture } = await render(DeploymentBudgetComponent, { inputs: { target: TARGET, strategyInstanceId: 'stopped-a', revision: 1 },
      providers: [{ provide: BrokerV2PanelService, useValue: { getBudget } }] });
    await screen.findByText('Fee evidence is incomplete.');
    expect(screen.getByText('Free budget').nextElementSibling?.textContent).toBe('Unknown');
    fixture.componentRef.setInput('revision', 2);
    await fixture.whenStable();
    expect(getBudget).toHaveBeenCalledTimes(2);
  });
});
