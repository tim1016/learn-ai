import type { SetupRequest } from '../options-backtest/options-backtest.types';
import { PositionPreviewService } from '../options-backtest/position/position-preview.service';
import { quietPositionPreview } from '../options-backtest/position/testing';
import { readBackOf, SESSION_CLOCK, SERVED_LIMITS } from '../options-backtest/testing/fixtures';
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
  const setups = { sessionClock: async () => SESSION_CLOCK, limits: async () => SERVED_LIMITS, list: async () => [servedSaved(declaration.setup_name, declaration.setup)], save: vi.fn(async (name: string, body: SetupRequest) => servedSaved(name, readBackOf(body))) };
  const view = await render(OptionsGoldenSearchPageComponent, { inputs, providers: [{ provide: PositionPreviewService, useValue: quietPositionPreview }, provideRouter([]), ...provideFakeDateWorld(), { provide: OptionsBacktestService, useValue: setups }, { provide: OptionsGoldenSearchService, useValue: fake }] });
  return { view, fake, setups };
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
  it('opens the selected saved setup in the editor', async () => {
    await page(service(), { setup_name: 'Saved two-leg setup' });
    const link = await screen.findByRole('link', { name: 'Edit setup and name in the setup editor', hidden: true });
    expect(link.getAttribute('href')).toBe('/options-backtest?setup_name=Saved%20two-leg%20setup');
  });

  it('renders lightweight study history without loading study evidence', async () => {
    const fake = service();
    const summaries = [{ id: 'saved-study', setup_name: 'Past setup', underlying: 'SPY', phase: 'compare', status: 'ready', updated_at_ms: 1791294000000 }];
    const view = await render(OptionsGoldenSearchPageComponent, { providers: [{ provide: PositionPreviewService, useValue: quietPositionPreview }, provideRouter([]), ...provideFakeDateWorld(), { provide: OptionsBacktestService, useValue: { list: async () => [] } }, { provide: OptionsGoldenSearchService, useValue: { ...fake, list: async () => summaries } }] });
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


it('blocks the checked plan on leg edits and uses the newly saved setup identity', async () => {
  const { fake, setups, user } = await preparePlan();
  await user.click(screen.getByRole('button', { name: 'Check plan' }));
  const start = screen.getByRole('button', { name: 'Start search' });
  await waitFor(() => expect(start).toHaveProperty('disabled', false));
  await user.click(screen.getByRole('button', { name: 'Flip Leg 1 to Put' }));
  expect(start).toHaveProperty('disabled', true);
  expect(screen.getByRole('button', { name: 'Check plan' })).toHaveProperty('disabled', true);
  const changed = { ...declarations(), setup_name: 'Edited position', setup_hash: 'e'.repeat(64), setup: { ...declarations().setup, legs: declarations().setup.legs.map(leg => ({ ...leg, right: 'put' as const })) } };
  fake.declarations.mockResolvedValue(changed);
  const name = screen.getByRole('textbox', { name: 'Save edited setup as' });
  fireEvent.input(name, { target: { value: 'Edited position' } });
  await user.click(screen.getByRole('button', { name: 'Save position for search' }));
  await waitFor(() => expect(fake.declarations).toHaveBeenLastCalledWith('Edited position'));
  expect(setups.save).toHaveBeenCalledWith('Edited position', expect.objectContaining({ legs: expect.arrayContaining([expect.objectContaining({ leg_id: 'call', right: 'put' })]) }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Save position for search' })).toBeNull());
  expect(start).toHaveProperty('disabled', true);
});


it('explains invalid position fields and restores save after the user corrects them', async () => {
  await page(service(), { setup_name: 'Saved two-leg setup' });
  const contracts = await screen.findByRole('spinbutton', { name: 'Leg 1 contracts' });
  fireEvent.input(contracts, { target: { value: '0' } });
  await screen.findByText(/Enter a whole number of contracts/);
  const save = screen.getByRole('button', { name: 'Save position for search' });
  expect(save).toHaveProperty('disabled', true);
  fireEvent.input(contracts, { target: { value: '2' } });
  await waitFor(() => expect(save).toHaveProperty('disabled', false));
  expect(screen.queryByText(/Enter a whole number of contracts/)).toBeNull();
  const offset = screen.getByRole('spinbutton', { name: 'Leg 1 strike offset' });
  fireEvent.input(offset, { target: { value: '1.5' } });
  await screen.findByText(/Enter a whole number of steps/);
  expect(save).toHaveProperty('disabled', true);
  fireEvent.input(offset, { target: { value: '2' } });
  await waitFor(() => expect(save).toHaveProperty('disabled', false));
});

it('keeps a setup name edited while the save declarations are pending', async () => {
  const fake = service();
  await page(fake, { setup_name: 'Saved two-leg setup' });
  fireEvent.click(await screen.findByRole('button', { name: 'Flip Leg 1 to Put' }));
  const name = screen.getByRole('textbox', { name: 'Save edited setup as' });
  fireEvent.input(name, { target: { value: 'Earlier name' } });
  let finish: (() => void) | undefined;
  const pending = new Promise<void>(resolve => { finish = resolve; });
  fake.declarations.mockImplementationOnce(async () => {
    await pending;
    return { ...declarations(), setup_name: 'Earlier name', setup_hash: 'e'.repeat(64) };
  });
  fireEvent.click(screen.getByRole('button', { name: 'Save position for search' }));
  await waitFor(() => expect(fake.declarations).toHaveBeenLastCalledWith('Earlier name'));
  fireEvent.input(name, { target: { value: 'Newer name' } });
  finish?.();
  await screen.findByText('The earlier position was saved, but newer edits remain. Save them before searching.');
  expect(screen.getByRole('textbox', { name: 'Save edited setup as' })).toHaveProperty('value', 'Newer name');
  expect(screen.getByRole('button', { name: 'Save position for search' })).toHaveProperty('disabled', false);
  expect(screen.getByRole('button', { name: 'Check plan' })).toHaveProperty('disabled', true);
});
