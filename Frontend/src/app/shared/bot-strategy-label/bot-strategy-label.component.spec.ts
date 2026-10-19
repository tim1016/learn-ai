import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GoldenOptionSetupsService } from '../../components/options-backtest/golden-option-setups.service';
import { BotStrategyLabelComponent } from './bot-strategy-label.component';

const OPTIONS_BOT = { strategy_label: 'Options bot', options_bot: { golden_setup_id: 'ogs_68' } };

function goldenSetups(list: () => Promise<unknown[]>) {
  return { provide: GoldenOptionSetupsService, useValue: { list: vi.fn(list) } };
}

describe('BotStrategyLabelComponent', () => {
  it('names an Options bot by its approved setup, so two bots on one symbol read apart', async () => {
    await render(BotStrategyLabelComponent, {
      inputs: { bot: OPTIONS_BOT },
      providers: [goldenSetups(async () => [{ id: 'ogs_60', name: 'SPY setup from run 60' }, { id: 'ogs_68', name: 'SPY setup from run 68' }])],
    });

    expect(await screen.findByText('SPY setup from run 68')).toBeTruthy();
  });

  it('keeps the strategy label when the setup is not listed or the names cannot be read', async () => {
    const { rerender, fixture } = await render(BotStrategyLabelComponent, {
      inputs: { bot: OPTIONS_BOT },
      providers: [goldenSetups(async () => { throw new Error('evidence unavailable'); })],
    });
    await fixture.whenStable();
    expect(screen.getByText('Options bot')).toBeTruthy();

    await rerender({ inputs: { bot: { ...OPTIONS_BOT, options_bot: { golden_setup_id: 'ogs_gone' } } } });
    expect(screen.getByText('Options bot')).toBeTruthy();
  });

  it('reads no setup names for a Signal bot', async () => {
    const list = vi.fn(async () => []);

    await render(BotStrategyLabelComponent, {
      inputs: { bot: { strategy_label: 'EMA Crossover Signal', options_bot: null } },
      providers: [{ provide: GoldenOptionSetupsService, useValue: { list } }],
    });

    expect(screen.getByText('EMA Crossover Signal')).toBeTruthy();
    expect(list).not.toHaveBeenCalled();
  });
});
