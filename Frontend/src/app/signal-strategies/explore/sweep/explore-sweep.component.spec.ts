import { TestBed } from '@angular/core/testing';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it } from 'vitest';

import { harnessEntry, registryEntry } from '../../testing/fixtures';
import { emptyWorld } from '../../testing/signal-strategy-world';
import { EMA, SAVED_RUN_POLICY, savedRun, savedSearch, sweepableEma } from '../testing/explore-fixtures';
import { provideExploreWorld, type ExploreWorld } from '../testing/explore-world';

afterEach(() => {
  TestBed.resetTestingModule();
});

async function open(url: string, evidence: Partial<ExploreWorld> = {}) {
  const { providers, calls } = provideExploreWorld(
    emptyWorld({ entries: [registryEntry()], schemas: [sweepableEma()] }),
    { runs: {}, searches: {}, ...evidence },
  );
  TestBed.configureTestingModule({ providers });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return { harness, calls };
}

describe('Sweep', () => {
  it('opens a search from the address, with the way on to Walk-forward, and starts nothing', async () => {
    const { calls } = await open(`/signal-strategies/${EMA}/explore/sweep?search=grid-search-0001`, {
      searches: { 'grid-search-0001': savedSearch() },
    });

    const link = await screen.findByRole('link', { name: /Test this grid out of sample in Walk-forward/ });

    expect(link.getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/walk-forward?search=grid-search-0001`);
    expect(calls.jobs.started).toEqual([]);
  });

  it('refuses a search another strategy ran, names its owner and shows none of its result', async () => {
    await open(`/signal-strategies/${EMA}/explore/sweep?search=grid-search-0002`, {
      searches: { 'grid-search-0002': savedSearch({ id: 'grid-search-0002', strategy_key: 'rsi_mean_reversion' }) },
    });

    expect(await screen.findByText(/belongs to rsi_mean_reversion, not ema_crossover_signal/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open it under rsi_mean_reversion' }).getAttribute('href')).toBe(
      '/signal-strategies/rsi_mean_reversion/explore/sweep?search=grid-search-0002',
    );
    expect(screen.queryByText(/Every number on this page is/)).toBeNull();
  });

  it('says a search that does not exist was not found, keeping its id', async () => {
    await open(`/signal-strategies/${EMA}/explore/sweep?search=nope`);

    expect(await screen.findByText('Grid search nope was not found.')).toBeTruthy();
  });

  it('fills the new-search form from a saved run, says what arrived and what did not, and starts nothing (T12)', async () => {
    const run = savedRun({ requestedEngine: 'both', commissionPerOrder: null, dataPolicy: { ...SAVED_RUN_POLICY, adjusted: false } });
    const { calls } = await open(`/signal-strategies/${EMA}/explore/sweep?run=91`, { runs: { 91: run } });

    const notice = await screen.findByRole('region', { name: 'Started from' });
    const arrived = within(notice);

    expect(arrived.getByRole('link', { name: 'backtest run #91' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/backtest?run=91`);
    expect(arrived.getByText('Symbol').nextElementSibling?.textContent).toBe('QQQ');
    expect(arrived.getByText('Window').nextElementSibling?.textContent).toBe('2026-03-02 to 2026-04-02');
    expect(arrived.getByText('Settings').nextElementSibling?.textContent).toContain('fast_period = 8');
    expect(arrived.getByText('Starting capital').nextElementSibling?.textContent).toBe('75000');
    // What a sweep cannot take is listed with its reason, not dropped.
    expect(arrived.getByText('Engine').parentElement?.textContent).toContain('Sweep runs the Python engine only');
    expect(arrived.getByText('Adjustment').parentElement?.textContent).toContain('reads split-adjusted bars only');
    expect(arrived.getByText('Commission').parentElement?.textContent).toContain('starts at 0');
    expect(arrived.getByText('Slippage')).toBeTruthy();

    // The form takes the seed and the server is asked whether it is valid; no job starts.
    await waitFor(() => expect(calls.sweepPreflights.length).toBeGreaterThan(0));
    const asked = calls.sweepPreflights[calls.sweepPreflights.length - 1];
    expect(asked).toMatchObject({
      strategy_key: EMA,
      symbol: 'QQQ',
      initial_cash: 75_000,
      commission_per_order: 0,
      param_ranges: { fast_period: { type: 'value_list', values: [8] }, slow_period: { type: 'value_list', values: [21] } },
    });
    expect(calls.jobs.started).toEqual([]);
    expect(screen.getByRole('button', { name: /Launch search/ }).hasAttribute('disabled')).toBe(true);
  });

  it('refuses a handoff from a run another strategy produced, leaving the form empty', async () => {
    const { calls } = await open(`/signal-strategies/${EMA}/explore/sweep?run=92`, {
      runs: { 92: savedRun({ id: 92, strategyName: 'rsi_mean_reversion' }) },
    });

    expect(await screen.findByText(/Saved run #92 belongs to rsi_mean_reversion, not ema_crossover_signal/)).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Started from' })).toBeNull();
    expect(calls.jobs.started).toEqual([]);
  });

  it('says a run that does not exist was not found', async () => {
    await open(`/signal-strategies/${EMA}/explore/sweep?run=404`);

    expect(await screen.findByText('Saved run #404 was not found.')).toBeTruthy();
  });

  it('shows the registry’s reason where it does not allow a sweep, and offers no form', async () => {
    const { providers } = provideExploreWorld(
      emptyWorld({ entries: [harnessEntry()], schemas: [] }),
    );
    TestBed.configureTestingModule({ providers });
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl('/signal-strategies/deployment_validation/explore/sweep');

    expect(await screen.findByText('The registry does not allow a sweep of this strategy.')).toBeTruthy();
    expect(screen.getAllByText('This strategy is an operational harness, not a production candidate.').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /Launch search/ })).toBeNull();
  });
});
