import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GoldenOptionSetupsService } from '../../components/options-backtest/golden-option-setups.service';
import { BotStrategyLabelComponent } from './bot-strategy-label.component';

const NAMES: Record<string, string> = { ogs_60: 'SPY setup from run 60', ogs_68: 'SPY setup from run 68' };

function optionsBot(goldenSetupId: string) {
  return { strategy_label: 'Options bot', options_bot: { golden_setup_id: goldenSetupId } };
}

function goldenSetups() {
  const get = vi.fn(async (id: string) => {
    if (!(id in NAMES)) throw new Error('not found');
    return { id, name: NAMES[id] };
  });
  return { get, provider: { provide: GoldenOptionSetupsService, useValue: { get } } };
}

describe('BotStrategyLabelComponent', () => {
  it('names an Options bot by its approved setup, so two bots on one symbol read apart', async () => {
    const { provider } = goldenSetups();

    await render(BotStrategyLabelComponent, { inputs: { bot: optionsBot('ogs_68') }, providers: [provider] });

    expect(await screen.findByText('SPY setup from run 68')).toBeTruthy();
  });

  it('names a setup it had not read before, as a bot deployed after the page opened does', async () => {
    const { provider, get } = goldenSetups();
    const { rerender } = await render(BotStrategyLabelComponent, { inputs: { bot: optionsBot('ogs_68') }, providers: [provider] });
    await screen.findByText('SPY setup from run 68');

    await rerender({ inputs: { bot: optionsBot('ogs_60') } });

    expect(await screen.findByText('SPY setup from run 60')).toBeTruthy();
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('keeps the strategy label for a setup it cannot read', async () => {
    const { provider, get } = goldenSetups();
    const { fixture } = await render(BotStrategyLabelComponent, { inputs: { bot: optionsBot('ogs_gone') }, providers: [provider] });
    await fixture.whenStable();

    expect(get).toHaveBeenCalledWith('ogs_gone');
    expect(screen.getByText('Options bot')).toBeTruthy();
  });

  it('reads no setup names for a Signal bot', async () => {
    const { provider, get } = goldenSetups();

    await render(BotStrategyLabelComponent, {
      inputs: { bot: { strategy_label: 'EMA Crossover Signal', options_bot: null } },
      providers: [provider],
    });

    expect(screen.getByText('EMA Crossover Signal')).toBeTruthy();
    expect(get).not.toHaveBeenCalled();
  });
});
