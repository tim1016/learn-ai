import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import {
  DeployTradedAcknowledgementComponent,
  type TradedAssetWarning,
} from './deploy-traded-acknowledgement.component';

const WARNING: TradedAssetWarning = {
  headline: 'Tested trading SPY. Untested trading QQQ.',
  detail: 'The detail the What step shows.',
  acceptance_label: 'I accept that this strategy is untested trading QQQ.',
};

describe('DeployTradedAcknowledgementComponent', () => {
  it("asks for the backend's own sentence to be ticked, with a reason", async () => {
    const user = userEvent.setup();
    const acknowledgedChange = vi.fn();
    const reasonChange = vi.fn();
    await render(DeployTradedAcknowledgementComponent, {
      inputs: { warning: WARNING },
      on: { acknowledgedChange, reasonChange },
    });

    screen.getByText('Tested trading SPY. Untested trading QQQ.');
    await user.click(screen.getByRole('checkbox', { name: 'I accept that this strategy is untested trading QQQ.' }));
    await user.type(screen.getByRole('textbox', { name: 'Your reason' }), 'QQQ');

    expect(acknowledgedChange).toHaveBeenCalledWith(true);
    expect(reasonChange).toHaveBeenLastCalledWith('QQQ');
    screen.getByText('Kept with this bot for good.');
  });

  it('shows a Dry Run the warning with nothing to tick and no reason to give', async () => {
    await render(DeployTradedAcknowledgementComponent, {
      inputs: { warning: { ...WARNING, acceptance_label: null } },
    });

    screen.getByText('Tested trading SPY. Untested trading QQQ.');
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('says what is wrong with the reason next to it', async () => {
    await render(DeployTradedAcknowledgementComponent, {
      inputs: { warning: WARNING, reasonError: 'Give at least 10 characters.' },
    });

    expect(screen.getByRole('alert').textContent).toContain('Give at least 10 characters.');
    expect(screen.getByRole('textbox', { name: 'Your reason' }).getAttribute('aria-invalid')).toBe('true');
  });
});
