import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { BrokerV2CardHelpButtonComponent } from './broker-v2-card-help-button.component';
import { BrokerV2HelpDrawerService } from './broker-v2-help-drawer.service';

describe('BrokerV2CardHelpButtonComponent', () => {
  it('opens the mapped manual anchor for the card', async () => {
    const open = vi.fn();
    await render(BrokerV2CardHelpButtonComponent, {
      inputs: { cardName: 'holds', label: 'Help: holds' },
      providers: [{ provide: BrokerV2HelpDrawerService, useValue: { open } }],
    });

    screen.getByRole('button', { name: 'Help: holds' }).click();

    expect(open).toHaveBeenCalledWith('hold-actions');
  });
});
