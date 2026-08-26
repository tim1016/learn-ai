import { render, screen, fireEvent } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { fakeCatalogBot } from '../../../../testing/bot-panel-fixtures';
import type { BotCatalogView, PanelAction } from '../lib/broker-v2-panel.types';
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

  // ── S2/S4 (#1778): the roster's own recovery command ──────────────────────
  // `row_action` shipped on the wire with zero frontend references, so an
  // attention row named a problem and offered no way to act on it.

  const rowAction: PanelAction = {
    action_id: 'cancel_verified_working_orders',
    label: 'Cancel working orders',
    explanation: 'Cancel the orders the Clerk can still prove it owns.',
    enabled: true,
    blockers: [],
    confirmation: null,
    revision: 42,
    concurrency_token: 'row-token',
  };

  it('offers an attention row its backend-authored recovery command', async () => {
    const rowActionRequested = vi.fn();
    const bot = fakeCatalogBot({
      strategy_instance_id: 'crashed-bot',
      status_label: 'Crashed',
      needs_attention: true,
      running: false,
      row_action: rowAction,
    });
    await render(BotsRosterComponent, {
      componentInputs: { bots: [bot], selectedSid: null },
      componentOutputs: { rowActionRequested: { emit: rowActionRequested } as never },
    });

    fireEvent.click(
      await screen.findByRole('button', { name: 'Cancel working orders' }),
    );

    expect(rowActionRequested).toHaveBeenCalledWith({ bot, action: rowAction });
  });

  it('keeps the recovery command off rows the backend authored none for', async () => {
    // A healthy row is not a place to offer a recovery mutation, and the
    // backend is where that is decided (`_catalog_row_action`, proven by
    // `test_a_healthy_row_carries_no_recovery_command`). The rail renders
    // what arrived rather than re-deriving the gate — an earlier local gate
    // keyed on the rail's own grouping is what silently threw a retired
    // bot's authored cure away (#1778).
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'healthy-bot', row_action: null }),
    ]);

    await screen.findByText('healthy-bot');
    expect(screen.queryByRole('button', { name: 'Cancel working orders' })).toBeNull();
  });

  it('renders an attention row honestly when no command is offered', async () => {
    await renderRail([
      fakeCatalogBot({ strategy_instance_id: 'stuck-bot', needs_attention: true }),
    ]);

    expect(await screen.findByText('stuck-bot')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel working orders' })).toBeNull();
  });

  it('keeps a row carrying a recovery command accessible', async () => {
    await renderRail([
      fakeCatalogBot({
        strategy_instance_id: 'crashed-bot',
        status_label: 'Crashed',
        needs_attention: true,
        running: false,
        row_action: rowAction,
      }),
    ]);
    await screen.findByRole('button', { name: 'Cancel working orders' });

    // `region` is a harness artifact: the rail is a fragment rendered without
    // the route shell that supplies its landmark, and it flags pre-existing
    // markup identically. Contrast is checked visually, as elsewhere here.
    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it('tones a crashed row as an alert rather than muting it', async () => {
    // The row named the crash in its detail line but rendered its state in
    // the same muted tone a flat, deliberately-stopped bot uses.
    const { container } = await renderRail([
      fakeCatalogBot({
        strategy_instance_id: 'crashed-bot',
        status_label: 'Crashed',
        needs_attention: true,
        running: false,
      }),
    ]);

    await screen.findByText('crashed-bot');
    const value = container.querySelector('.rail-row__value');
    expect(value?.getAttribute('data-tone')).toBe('alert');
  });

  it('offers a retired row its recovery command instead of dropping it', async () => {
    // A RETIRED bot can still hold stranded exposure. The backend sets
    // `needs_attention` regardless of phase and authors `row_action` for it;
    // grouping RETIRED before attention silently threw the cure away and left
    // the operator at a dead end.
    const rowActionRequested = vi.fn();
    const bot = fakeCatalogBot({
      strategy_instance_id: 'retired-bot',
      status_label: 'Retired',
      phase: 'RETIRED',
      needs_attention: true,
      running: false,
      row_action: rowAction,
    });
    await render(BotsRosterComponent, {
      componentInputs: { bots: [bot], selectedSid: null },
      componentOutputs: { rowActionRequested: { emit: rowActionRequested } as never },
    });

    fireEvent.click(
      await screen.findByRole('button', { name: 'Cancel working orders' }),
    );

    expect(rowActionRequested).toHaveBeenCalledWith({ bot, action: rowAction });
  });

  it('tones a retired row that needs attention as an alert', async () => {
    // Same defect as the crashed row: `muted` is the tone that made a bot in
    // trouble read like a deliberate stop.
    const { container } = await renderRail([
      fakeCatalogBot({
        strategy_instance_id: 'retired-bot',
        status_label: 'Retired',
        phase: 'RETIRED',
        needs_attention: true,
        running: false,
      }),
    ]);

    await screen.findByText('retired-bot');
    const value = container.querySelector('.rail-row__value');
    expect(value?.getAttribute('data-tone')).toBe('alert');
  });

  it('keeps a retired row that is genuinely quiet muted', async () => {
    // The other half of the contract: retirement is not by itself alarming.
    const { container } = await renderRail([
      fakeCatalogBot({
        strategy_instance_id: 'retired-quiet',
        status_label: 'Retired',
        phase: 'RETIRED',
        running: false,
      }),
    ]);

    await screen.findByText('retired-quiet');
    const value = container.querySelector('.rail-row__value');
    expect(value?.getAttribute('data-tone')).toBe('muted');
  });

  it('tones the row command from the canonical action map, not a fixed danger', async () => {
    // `reconcile_now` is a refresh and `open_custody_timeline` is read-only.
    // Painting whatever the backend picked as primary in destructive red
    // taught the operator that red means nothing.
    const { container } = await renderRail([
      fakeCatalogBot({
        strategy_instance_id: 'stale-bot',
        needs_attention: true,
        row_action: { ...rowAction, action_id: 'reconcile_now', label: 'Reconcile now' },
      }),
    ]);

    await screen.findByRole('button', { name: 'Reconcile now' });
    const command = container.querySelector('.rail-row__action .panel-action__button');
    expect(command?.className).toContain('panel-action__button--neutral');
    expect(command?.className).not.toContain('panel-action__button--danger');
  });

  it('keeps a genuinely destructive row command toned as danger', async () => {
    const { container } = await renderRail([
      fakeCatalogBot({
        strategy_instance_id: 'flatten-bot',
        needs_attention: true,
        row_action: { ...rowAction, action_id: 'execute_safe_flatten', label: 'Flatten now' },
      }),
    ]);

    await screen.findByRole('button', { name: 'Flatten now' });
    const command = container.querySelector('.rail-row__action .panel-action__button');
    expect(command?.className).toContain('panel-action__button--danger');
  });
});
