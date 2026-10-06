import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeBotPanelView } from '../../../../testing/bot-panel-fixtures';
import { BotSetupComponent } from './bot-setup.component';

const WARNING = {
  headline: 'Tested trading SPY. Untested trading QQQ.',
  detail: 'This strategy has not been tested buying QQQ.',
  acceptance_label: null,
};

describe('BotSetupComponent', () => {
  it("shows Deploy's warning and what the owner accepted, for a bot that trades something else (#2912)", async () => {
    await render(BotSetupComponent, {
      inputs: {
        panel: fakeBotPanelView({
          traded: { kind: 'stock', symbol: 'QQQ' },
          traded_asset_warning: WARNING,
          traded_asset_acknowledgement: {
            acknowledgement: 'I_ACCEPT_UNTESTED_TRADED_ASSET_RISK',
            reason: 'QQQ has followed SPY closely enough to try this.',
          },
        }),
      },
    });

    screen.getByText('Untested traded asset');
    screen.getByText('Tested trading SPY. Untested trading QQQ.');
    screen.getByText('This strategy has not been tested buying QQQ.');
    screen.getByText(/QQQ has followed SPY closely enough to try this\./);
  });

  it('shows a Dry Run the warning with no acceptance, since it gave none', async () => {
    await render(BotSetupComponent, {
      inputs: { panel: fakeBotPanelView({ traded: { kind: 'stock', symbol: 'QQQ' }, traded_asset_warning: WARNING }) },
    });

    screen.getByText('Tested trading SPY. Untested trading QQQ.');
    expect(screen.queryByText(/You accepted this at Deploy/)).toBeNull();
  });

  it('says nothing of it for a bot that trades what it listens to', async () => {
    await render(BotSetupComponent, { inputs: { panel: fakeBotPanelView() } });

    expect(screen.queryByText('Untested traded asset')).toBeNull();
  });
});
