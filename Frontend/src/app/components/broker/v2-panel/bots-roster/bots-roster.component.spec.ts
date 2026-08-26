import { render, screen, fireEvent } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeCatalogBot } from '../../../../testing/bot-panel-fixtures';
import type { BotCatalogView } from '../lib/broker-v2-panel.types';
import { BotsRosterComponent } from './bots-roster.component';

async function renderRail(
  bots: BotCatalogView[],
  inputs: { selectedSid?: string | null } = {},
) {
  return render(BotsRosterComponent, {
    componentInputs: { bots, selectedSid: inputs.selectedSid ?? null },
  });
}

function searchBox(): HTMLElement {
  return screen.getByRole('searchbox', {
    name: 'Filter bots by name, symbol, or strategy',
  });
}

describe('BotsRosterComponent', () => {
  it('renders all bot rows', async () => {
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'spy-01', symbol: 'SPY' }),
      fakeCatalogBot({ strategy_instance_id: 'qqq-01', symbol: 'QQQ' }),
    ]);

    expect(await screen.findByText('spy-01')).toBeTruthy();
    expect(screen.getByText('qqq-01')).toBeTruthy();
  });

  it('filters by search term', async () => {
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'spy-01', symbol: 'SPY' }),
      fakeCatalogBot({ strategy_instance_id: 'qqq-01', symbol: 'QQQ' }),
    ]);

    fireEvent.input(searchBox(), { target: { value: 'qqq' } });

    expect(await screen.findByText('qqq-01')).toBeTruthy();
    expect(screen.queryByText('spy-01')).toBeNull();
  });

  it('filters by the human strategy label', async () => {
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'validation-01', strategy_label: 'Opening Range Breakout' }),
      fakeCatalogBot({ strategy_instance_id: 'other-01', strategy_label: 'Moving Average Crossover' }),
    ]);

    fireEvent.input(searchBox(), { target: { value: 'opening range breakout' } });

    expect(await screen.findByText('validation-01')).toBeTruthy();
    expect(screen.queryByText('other-01')).toBeNull();
  });

  it('groups bots by attention, running, and stopped with whole-fleet counts', async () => {
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
      fakeCatalogBot({ strategy_instance_id: 'running-bot' }),
      fakeCatalogBot({ strategy_instance_id: 'stopped-bot', running: false, phase: 'OFF_DUTY' }),
    ]);

    expect(await screen.findByRole('heading', { name: 'Needs attention · 1' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Running · 1' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Stopped · 1' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Needs attention 1' })).toBeTruthy();
  });

  it('narrows to one group when its chip is pressed, and clears when pressed again', async () => {
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
      fakeCatalogBot({ strategy_instance_id: 'running-bot' }),
    ]);

    const chip = await screen.findByRole('button', { name: 'Needs attention 1' });
    fireEvent.click(chip);

    expect(await screen.findByText('urgent-bot')).toBeTruthy();
    expect(screen.queryByText('running-bot')).toBeNull();
    expect(chip.getAttribute('aria-pressed')).toBe('true');

    fireEvent.click(chip);

    expect(await screen.findByText('running-bot')).toBeTruthy();
    expect(chip.getAttribute('aria-pressed')).toBe('false');
  });

  /** Retired bots have no chip in the design, but must not vanish from the fleet. */
  it('still groups retired bots, and only then offers their chip', async () => {
    await renderRail([fakeCatalogBot({ strategy_instance_id: 'gone-bot', phase: 'RETIRED', running: false })]);

    expect(await screen.findByRole('heading', { name: 'Retired · 1' })).toBeTruthy();
    expect(screen.getByText('gone-bot')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retired 1' })).toBeTruthy();
  });

  it('omits the retired chip when no bot is retired', async () => {
    await renderRail([fakeCatalogBot({ strategy_instance_id: 'spy-01' })]);

    await screen.findByText('spy-01');
    expect(screen.queryByRole('button', { name: /Retired/ })).toBeNull();
  });

  it('renders a dedicated empty-fleet state', async () => {
    await renderRail([]);
    expect(await screen.findByText(/No Alpaca bots yet/i)).toBeTruthy();
  });

  it('renders a dedicated empty-filter state', async () => {
    await renderRail([fakeCatalogBot()]);
    fireEvent.input(searchBox(), { target: { value: 'missing' } });
    expect(await screen.findByText(/No bots match this filter/i)).toBeTruthy();
  });

  it('carries the backend status label and derived facts on each row', async () => {
    await renderRail([
      fakeCatalogBot({ status_label: 'Working', exposure: { SPY: 12 }, fills_today: 3 }),
    ]);

    expect(await screen.findByText('Working · +12 SPY · 3 fills')).toBeTruthy();
  });

  it('names a crashed run instead of showing it as an ordinary off-duty row', async () => {
    // S3b: three bots died during the fleet run and every row read
    // 'Off duty · Flat'. The backend now labels an unclean exit from the
    // shared operator vocabulary, so the row has to say so.
    await renderRail([
      fakeCatalogBot({
        strategy_instance_id: 'crashed-bot',
        status_label: 'Crashed',
        needs_attention: true,
        running: false,
        exposure: {},
      }),
    ]);

    expect(await screen.findByText(/^Crashed · /)).toBeTruthy();
    expect(screen.queryByText(/^Off duty · /)).toBeNull();
  });

  it('orders attention bots ahead of running bots', async () => {
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'normal-bot' }),
      fakeCatalogBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
    ]);

    await screen.findByText('urgent-bot');
    const bodyText = document.body.textContent ?? '';
    expect(bodyText.indexOf('urgent-bot')).toBeLessThan(bodyText.indexOf('normal-bot'));
  });

  it('marks the selected row and emits the sid on click', async () => {
    const botSelected = vi.fn();
    await render(BotsRosterComponent, {
      componentInputs: { bots: [fakeCatalogBot({ strategy_instance_id: 'spy-01' })], selectedSid: 'spy-01' },
      componentOutputs: { botSelected: { emit: botSelected } as never },
    });

    const row = await screen.findByRole('button', { name: /spy-01/ });
    expect(row.getAttribute('aria-current')).toBe('true');

    fireEvent.click(row);
    expect(botSelected).toHaveBeenCalledWith('spy-01');
  });
});
