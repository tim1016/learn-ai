import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import type { BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';

const RECEIPT: BudgetDeployReceipt = {
  outcome: 'success',
  status: 'deployed',
  receipt_id: 'command-spy-ema-20260929-0931',
  recorded_at_ms: 1_700_000_000_000,
  first_deployed_at_ms: 1_700_000_000_000,
  account_id: 'PA9',
  command_id: 'command-1',
  committed_usd: '1000.00',
  strategy_instance_id: 'spy-ema-20260929-0931',
  run_id: 'run-1',
  world: 'real_paper',
  message: 'spy-ema-20260929-0931 is deployed',
  explanation: '$1000.00 is set aside for it.',
  next_action: 'Open the bot\'s page to watch it trade.',
  replaces_strategy_instance_id: null,
};

const BOT_LINK = {
  commands: ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'bots', 'spy-ema-20260929-0931'],
  queryParams: { from: 'bots' },
};

describe('DeployLaunchReceiptComponent', () => {
  it('names the bot, the money set aside, the world and the account, and links the bot’s page', async () => {
    await render(DeployLaunchReceiptComponent, {
      providers: [provideRouter([])],
      inputs: { receipt: RECEIPT, botLink: BOT_LINK },
    });

    expect(screen.getByRole('heading', { name: RECEIPT.message })).toBeTruthy();
    expect(screen.getByText(RECEIPT.explanation)).toBeTruthy();
    expect(screen.getByText('spy-ema-20260929-0931')).toBeTruthy();
    expect(screen.getByText('Set aside').nextElementSibling?.textContent).toBe('$1,000.00');
    expect(screen.getByText('PAPER · practice money')).toBeTruthy();
    expect(screen.getByText('PA9')).toBeTruthy();
    expect(screen.getByText('Deployed')).toBeTruthy();
    expect(screen.getByText('First deployed')).toBeTruthy();
    expect(screen.queryByText('Replaces')).toBeNull();
    expect(screen.getByRole('link', { name: 'Open spy-ema-20260929-0931' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/spy-ema-20260929-0931?from=bots');
    // H12: the legacy bot-control path is gone.
    expect(screen.queryByRole('link', { name: 'Open bot control' })).toBeNull();
  });

  it('names the bot a Deploy again replaces', async () => {
    await render(DeployLaunchReceiptComponent, {
      providers: [provideRouter([])],
      inputs: { receipt: { ...RECEIPT, replaces_strategy_instance_id: 'spy-ema-20260925-1402' }, botLink: BOT_LINK },
    });

    expect(screen.getByText('Replaces').nextElementSibling?.textContent).toBe('spy-ema-20260925-1402');
  });

  it('never names the real account for a Dry Run, whose cash is simulated (H18)', async () => {
    await render(DeployLaunchReceiptComponent, {
      providers: [provideRouter([])],
      inputs: { receipt: { ...RECEIPT, world: 'synthetic', account_id: '318420190' }, botLink: null },
    });

    expect(screen.queryByText('318420190')).toBeNull();
    expect(screen.getByText('The bot’s own Dry Run account')).toBeTruthy();
    expect(screen.getByText('Simulated starting cash').nextElementSibling?.textContent).toBe('$1,000.00');
    expect(screen.getByText('DRY RUN · simulated cash')).toBeTruthy();
  });
});
