import { Router, provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { provideFakeDateWorld } from '../../shared/date-picker/testing/fake-trading-calendar';
import { servedSaved } from '../options-backtest/testing/fixtures';
import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import { OptionsGoldenSearchPageComponent } from './options-golden-search-page.component';
import { OptionsGoldenSearchService } from './options-golden-search.service';
import { declarations, preflight, study } from './testing';

function service() {
  return { declarations: vi.fn(async () => declarations()), list: vi.fn(async () => []), preflight: vi.fn(async () => preflight()), create: vi.fn(async () => study()), get: vi.fn(async () => study()), command: vi.fn(async () => study()) };
}

async function page(fake = service(), inputs = {}) {
  const declaration = declarations();
  const view = await render(OptionsGoldenSearchPageComponent, { inputs, providers: [provideRouter([]), ...provideFakeDateWorld(), { provide: OptionsBacktestService, useValue: { list: async () => [servedSaved(declaration.setup_name, declaration.setup)] } }, { provide: OptionsGoldenSearchService, useValue: fake }] });
  return { view, fake };
}

async function preparePlan() {
  const result = await page();
  const user = userEvent.setup();
  await screen.findByRole('option', { name: 'Saved two-leg setup' });
  await user.selectOptions(screen.getByRole('combobox', { name: 'Saved setup' }), 'Saved two-leg setup');
  await user.click(screen.getByRole('button', { name: 'Use saved setup' }));
  await screen.findByText(/Original identity/);
  await user.click(screen.getByText('Additional settings to vary'));
  const stop = within(screen.getByRole('region', { name: 'Stop loss' }));
  await user.click(stop.getByRole('button', { name: 'Explore' }));
  for (const [label, value] of [['Minimum', '20'], ['Maximum', '25'], ['Step', '5']]) fireEvent.input(stop.getByRole('textbox', { name: label }), { target: { value } });

  const dateFields = screen.getAllByRole('textbox').filter((field) => field.closest('app-date-range-picker') !== null);
  fireEvent.change(dateFields[0], { target: { value: '2024-01-02' } });
  fireEvent.blur(dateFields[0]);
  fireEvent.change(dateFields[1], { target: { value: '2026-09-18' } });
  fireEvent.blur(dateFields[1]);
  return { ...result, user };
}

describe('Options Golden Search workbench', () => {
  it('renders lightweight study history without loading study evidence', async () => {
    const fake = service();
    const summaries = [{ id: 'saved-study', setup_name: 'Past setup', underlying: 'SPY', phase: 'compare', status: 'ready', updated_at_ms: 1791294000000 }];
    const view = await render(OptionsGoldenSearchPageComponent, { providers: [provideRouter([]), ...provideFakeDateWorld(), { provide: OptionsBacktestService, useValue: { list: async () => [] } }, { provide: OptionsGoldenSearchService, useValue: { ...fake, list: async () => summaries } }] });
    expect(await screen.findByRole('link', { name: 'Past setup' })).toBeTruthy();
    expect(fake.get).not.toHaveBeenCalled();
    view.fixture.destroy();
  });

  it('sends exact selected values and UTC-ms history, then invalidates preflight on a policy edit', async () => {
    const { fake, user } = await preparePlan();
    const check = screen.getByRole('button', { name: 'Check plan' });
    await waitFor(() => expect(check).toHaveProperty('disabled', false));
    await user.click(check);
    await screen.findByText('Dollar limit: 2000 USD');
    expect(fake.preflight).toHaveBeenCalledWith(expect.objectContaining({ knobs: [{ path: 'all_legs.stop_loss', values: [{ call: { kind: 'percent', value: '20.0000' }, put: { kind: 'percent', value: '20.0000' } }, { call: { kind: 'percent', value: '25.0000' }, put: { kind: 'percent', value: '25.0000' } }], priority: 5, mode: 'search' }], history: expect.objectContaining({ start_ms: 1704171600000, end_ms: 1789790400000 }), policy: expect.objectContaining({ max_drawdown_percent: '20' }) }));
    const lock = screen.getByRole('button', { name: 'Start search' });
    expect(lock).toHaveProperty('disabled', false);
    await user.click(screen.getByText('Evidence requirements and additional limits'));
    const floor = screen.getByRole('spinbutton', { name: 'Annualized daily Sharpe floor' });
    await user.clear(floor);
    await user.type(floor, '2');
    expect(lock).toHaveProperty('disabled', true);
    expect(screen.queryByText('Dollar limit: 2000 USD')).toBeNull();
  });

  it('dispatches research before navigation can create a new page instance', async () => {
    const { fake, user, view } = await preparePlan();
    const navigate = vi.spyOn(view.fixture.debugElement.injector.get(Router), 'navigate').mockResolvedValue(true);
    await user.click(screen.getByRole('button', { name: 'Check plan' }));
    await user.click(screen.getByRole('button', { name: 'Start search' }));
    await waitFor(() => expect(navigate).toHaveBeenCalled());
    expect(fake.command.mock.invocationCallOrder[0]).toBeLessThan(navigate.mock.invocationCallOrder[0]);
  });

  it('offers the actual shared dimensions for pair audits', async () => {
    const { fake, user } = await preparePlan();
    const momentum = within(screen.getByRole('region', { name: 'Entry momentum' }));
    await user.click(momentum.getByRole('button', { name: 'Explore' }));
    for (const [label, value] of [['Minimum', '20'], ['Maximum', '25'], ['Step', '5']]) fireEvent.input(momentum.getByRole('textbox', { name: label }), { target: { value } });
    await user.click(screen.getByText('Search budget and advanced settings'));
    await user.click(screen.getByText('Predeclared pair audit'));
    await user.selectOptions(screen.getByRole('combobox', { name: 'First setting' }), 'all_legs.stop_loss');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Second setting' }), 'all_legs.momentum');
    await user.click(screen.getByRole('button', { name: 'Check plan' }));
    expect(fake.preflight).toHaveBeenCalledWith(expect.objectContaining({ pair_audits: [['all_legs.stop_loss', 'all_legs.momentum']] }));
  });

  it('sends the durable revision and reuses the command intent after a transport failure', async () => {
    const fake = service();
    fake.command.mockRejectedValue(new Error('Connection lost'));
    await page(fake, { studyId: 'study-1' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Run research' }));
    await screen.findByText('The command did not complete. Refresh to read the latest durable state.');
    await user.click(screen.getByRole('button', { name: 'Run research' }));
    expect(fake.command).toHaveBeenCalledTimes(2);
    expect(fake.command.mock.calls[0]).toEqual(fake.command.mock.calls[1]);
    expect(fake.command).toHaveBeenCalledWith('study-1', expect.objectContaining({ command: 'run_research', expected_revision: 4, idempotency_key: expect.any(String) }));
  });
});
