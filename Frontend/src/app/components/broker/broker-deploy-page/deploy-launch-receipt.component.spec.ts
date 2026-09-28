import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import type { BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';

const RECEIPT: BudgetDeployReceipt = {
  outcome: 'success',
  status: 'deployed',
  receipt_id: 'alpaca-paper-deploy:PA9:spy-test-01:1700000000000',
  recorded_at_ms: 1_700_000_000_000,
  account_id: 'PA9',
  command_id: 'command-1',
  committed_usd: '1000.00',
  strategy_instance_id: 'spy-test-01',
  run_id: 'run-1',
  world: 'real_paper',
  message: 'spy-test-01 is on duty in Alpaca paper.',
  explanation: 'The deployment binding is durable and Clerk governed.',
  next_action: 'Open the production bot control page.',
  panel_path: '/brokers/alpaca/accounts/PA9/bots/spy-test-01',
};

describe('DeployLaunchReceiptComponent', () => {
  it('renders the budget receipt fields', async () => {
    await render(DeployLaunchReceiptComponent, {
      providers: [provideRouter([])],
      inputs: { receipt: RECEIPT },
    });

    expect(screen.getByText(RECEIPT.message)).toBeTruthy();
    expect(screen.getByText(RECEIPT.explanation)).toBeTruthy();
    expect(screen.getByText(RECEIPT.receipt_id)).toBeTruthy();
    expect(screen.getByText('Deployed')).toBeTruthy();
    expect(screen.getByText('$1,000.00')).toBeTruthy();
    expect(screen.getByText(RECEIPT.strategy_instance_id)).toBeTruthy();
    expect(screen.getByText(RECEIPT.run_id)).toBeTruthy();
    expect(screen.getByText('Paper')).toBeTruthy();
    expect(screen.getByText(RECEIPT.account_id)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open bot control' }).getAttribute('href'))
      .toBe(RECEIPT.panel_path);
  });
});
