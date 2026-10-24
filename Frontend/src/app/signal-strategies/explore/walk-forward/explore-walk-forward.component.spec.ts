import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it } from 'vitest';

import { harnessEntry, registryEntry } from '../../testing/fixtures';
import { emptyWorld } from '../../testing/signal-strategy-world';
import { EMA, savedRun, savedSearch, sweepRequest, sweepableEma } from '../testing/explore-fixtures';
import { provideExploreWorld, type ExploreWorld } from '../testing/explore-world';
import { ExploreWalkForwardComponent } from './explore-walk-forward.component';

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

describe('Walk-forward', () => {
  it('fills the form from a sweep, carrying its grid, window and costs, and says its leader did not travel (T12)', async () => {
    const { calls } = await open(`/signal-strategies/${EMA}/explore/walk-forward?search=grid-search-0001`, {
      searches: { 'grid-search-0001': savedSearch() },
    });

    const notice = await screen.findByRole('region', { name: 'Started from' });
    const arrived = within(notice);

    expect(arrived.getByRole('link', { name: 'sweep grid-sea' }).getAttribute('href')).toBe(
      `/signal-strategies/${EMA}/explore/sweep?search=grid-search-0001`,
    );
    expect(arrived.getByText('Window').nextElementSibling?.textContent).toBe('2026-01-02 to 2026-06-30');
    expect(arrived.getByText('Grid').nextElementSibling?.textContent).toBe('fast_period 5 to 9 step 2');
    expect(arrived.getByText('Fixed settings').nextElementSibling?.textContent).toContain('slow_period = 21');
    expect(arrived.getByText('Commission per order').nextElementSibling?.textContent).toBe('0.5');
    expect(arrived.getByText('Starting capital').nextElementSibling?.textContent).toBe('50000');
    expect(arrived.getByText('Ranking').nextElementSibling?.textContent).toBe('total_return_pct, at least 8 trades');
    // What a walk-forward cannot take, and what it leaves to the owner, are in words.
    expect(arrived.getByText('Leader and cell results').parentElement?.textContent).toContain('picks its own winner on each training window');
    expect(arrived.getByText('Training and test lengths').parentElement?.textContent).toContain('choose how many months train');

    // The destination checks the seeded request against its own schema; nothing starts.
    await waitFor(() => expect(calls.walkForwardPreflights.length).toBeGreaterThan(0));
    expect(calls.walkForwardPreflights[calls.walkForwardPreflights.length - 1]).toMatchObject({
      ...sweepRequest(),
      training_months: 12,
      test_months: 3,
    });
    expect(calls.jobs.started).toEqual([]);
    expect(screen.getByRole('button', { name: /Launch study/ }).hasAttribute('disabled')).toBe(true);
  });

  it('fills the form from a saved run too, listing what a walk-forward cannot take from it', async () => {
    const run = savedRun({ requestedEngine: 'lean', fillMode: 'lean-sidecar' });
    const { calls } = await open(`/signal-strategies/${EMA}/explore/walk-forward?run=91`, { runs: { 91: run } });

    const arrived = within(await screen.findByRole('region', { name: 'Started from' }));

    expect(arrived.getByRole('link', { name: 'backtest run #91' })).toBeTruthy();
    expect(arrived.getByText('Engine').parentElement?.textContent).toContain('Walk-forward runs the Python engine only');
    expect(arrived.getByText('Fill mode').parentElement?.textContent).toContain('"lean-sidecar"');
    expect(arrived.getByText('Training and test lengths')).toBeTruthy();
    expect(calls.jobs.started).toEqual([]);
  });

  it('refuses a sweep another strategy ran and leaves the form empty', async () => {
    await open(`/signal-strategies/${EMA}/explore/walk-forward?search=grid-search-0002`, {
      searches: { 'grid-search-0002': savedSearch({ id: 'grid-search-0002', strategy_key: 'rsi_mean_reversion' }) },
    });

    expect(await screen.findByText(/belongs to rsi_mean_reversion, not ema_crossover_signal/)).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Started from' })).toBeNull();
  });

  it('opens a launched study in History by its job id', async () => {
    const { harness } = await open(`/signal-strategies/${EMA}/explore/walk-forward`);
    expect(await screen.findByRole('tab', { name: /new study/i })).toBeTruthy();

    const tool = harness.fixture.debugElement.query(By.directive(ExploreWalkForwardComponent)).componentInstance as ExploreWalkForwardComponent;
    await tool.onLaunched({ jobId: 'job-7' });

    await waitFor(() => expect(screen.getByRole('tab', { name: /history/i }).getAttribute('aria-selected')).toBe('true'));
  });

  it('shows the registry’s reason where it does not allow a walk-forward, and offers no form', async () => {
    const { providers } = provideExploreWorld(emptyWorld({ entries: [harnessEntry()], schemas: [] }));
    TestBed.configureTestingModule({ providers });
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl('/signal-strategies/deployment_validation/explore/walk-forward');

    expect(await screen.findByText('The registry does not allow a walk-forward of this strategy.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Launch study/ })).toBeNull();
  });
});
