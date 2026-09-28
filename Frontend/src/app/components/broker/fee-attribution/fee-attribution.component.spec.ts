import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ActivityPeriod } from '../../../api/alpaca.types';
import type { components } from '../../../api/broker.types';
import { resourceTarget } from '../../../fleet/resource-target';
import { BrokersService } from '../../../services/brokers.service';
import { FeeAttributionComponent } from './fee-attribution.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA1', bindingGeneration: 1, routingEpoch: 1 });
const VIEW: components['schemas']['DeploymentFeeAttribution'] = {
  account_id: 'PA1', observed_at_ms: 1_800_000_000_000, authority_revision: 8,
  available: true, known: true, account_unattributed_usd: '0', messages: [],
  rows: [
    { subject_id: 'bot:a', strategy_instance_id: 'a', label: 'Stopped bot A', estimated_usd: '0.02', modelled_settled_usd: '0', observed_usd: '0.05', total_usd: '0.07' },
    { subject_id: 'bot:b', strategy_instance_id: 'b', label: 'Bot B', estimated_usd: '0', modelled_settled_usd: '0.03', observed_usd: '0', total_usd: '0.03' },
  ],
};

async function show(
  view = VIEW,
  strategyInstanceId: string | null = null,
  options: { period?: ActivityPeriod | null; headingLevel?: 2 | 3 } = {},
) {
  const read = vi.fn().mockResolvedValue(view);
  await render(FeeAttributionComponent, {
    inputs: { target: TARGET, strategyInstanceId, ...options },
    providers: [{ provide: BrokersService, useValue: { getFeeAttribution: read } }],
  });
  return read;
}

describe('deployment fees', () => {
  it('shows exact server totals and distinguishes estimate, settlement and Alpaca charge', async () => {
    await show();
    await screen.findByText('Stopped bot A');
    expect(screen.getByText('Bot B')).toBeTruthy();
    expect(screen.getByText('$0.07')).toBeTruthy();
    expect(screen.getAllByText('Estimated')).toHaveLength(2);
    expect(screen.getAllByText('Settled')).toHaveLength(2);
    expect(screen.getAllByText('Charged by Alpaca')).toHaveLength(2);
    expect(screen.getAllByText('Total')).toHaveLength(2);
  });

  it('asks for the deployment authority and keeps account uncertainty visible on a bot', async () => {
    const read = await show({ ...VIEW, known: false, account_unattributed_usd: '1.25', messages: ['The complete fee population is unavailable. Reconcile account executions.'] }, 'a');
    await screen.findByText('Stopped bot A');
    expect(screen.queryByText('Bot B')).toBeNull();
    expect(screen.getByText(/Reconcile account executions/)).toBeTruthy();
    expect(screen.getByText(/Account charges not yet matched to a bot: \$1.25/)).toBeTruthy();
    expect(read).toHaveBeenCalledWith(TARGET, 'a', null);
    expect(screen.getByRole('heading', { name: 'Fees', level: 3 })).toBeTruthy();
  });

  it("reads one Activity period's account fees and names an outside order by its order number", async () => {
    const read = await show(
      {
        ...VIEW,
        period: '30d',
        period_start_ms: 1_797_400_000_000,
        rows: [
          ...VIEW.rows,
          {
            subject_id: 'external:ord-7f3a', strategy_instance_id: null, label: 'Outside order · MSFT', order_id: 'ord-7f3a',
            estimated_usd: '0', modelled_settled_usd: '0', observed_usd: '0.05', total_usd: '0.05',
          },
        ],
      },
      null,
      { period: '30d', headingLevel: 2 },
    );

    await screen.findByText('Outside order · MSFT');
    expect(read).toHaveBeenCalledWith(TARGET, null, '30d');
    expect(screen.getByText('ord-7f3a')).toBeTruthy();
    expect(screen.getByText(/Last 30 trading days/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Fees', level: 2 })).toBeTruthy();
  });

  it('never turns an unavailable projection into no fees', async () => {
    await show({ ...VIEW, available: false, known: false, rows: [], account_unattributed_usd: null, messages: ['Fee evidence is unavailable while the account Clerk is offline.'] });
    await screen.findByText(/account Clerk is offline/);
    expect(screen.queryByText('No fees have been recorded.')).toBeNull();
  });

  it('says a period had no fees only when the fee record is known', async () => {
    await show({ ...VIEW, rows: [], period: 'today', period_start_ms: 1_800_000_000_000 }, null, { period: 'today' });
    expect(await screen.findByText('No fees were charged in this period.')).toBeTruthy();
  });

  it('announces a failed fee read as an alert, like its sibling error rows', async () => {
    const read = vi.fn().mockRejectedValue(new Error('network down'));
    await render(FeeAttributionComponent, {
      inputs: { target: TARGET, strategyInstanceId: null },
      providers: [{ provide: BrokersService, useValue: { getFeeAttribution: read } }],
    });

    expect((await screen.findByRole('alert')).textContent).toContain('Fees could not be read. Refresh to retry.');
  });
});
