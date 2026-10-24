import { HttpErrorResponse } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, within } from '@testing-library/angular';
import { of, throwError } from 'rxjs';
import { afterEach, describe, expect, it } from 'vitest';

import { appConfig } from '../../../app.config';
import { GridSearchService } from '../../../components/grid-search/grid-search.service';
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { BacktestRunsService } from '../../../services/backtest-runs.service';
import { SignalStrategyRegistryService } from '../../signal-strategy-registry.service';
import { harnessEntry, registryEntry } from '../../testing/fixtures';
import type { StrategyRegistryEntry } from '../../signal-strategy-evidence';
import { EMA, savedRun, savedSearch } from '../testing/explore-fixtures';
import type { BacktestRunDetail } from '../../../services/backtest-runs.types';
import type { GridSearchDetail } from '../../../components/grid-search/grid-search.types';

const RSI = 'rsi_mean_reversion';

interface Records {
  entries?: readonly StrategyRegistryEntry[] | Error;
  runs?: Record<number, BacktestRunDetail>;
  searches?: Record<string, GridSearchDetail>;
  runFailure?: Error;
  /** Serves the run report's chart reads from the test controller, so none goes to the network. */
  reportReads?: boolean;
}

afterEach(() => {
  TestBed.resetTestingModule();
});

/** The real app routes in front of the records an old address names. */
function configure(records: Records): void {
  const fleet = provideFleetDirectory();
  const entries = records.entries ?? [registryEntry(), registryEntry({ strategy_key: RSI, display_name: 'RSI Mean Reversion' }), harnessEntry()];
  TestBed.configureTestingModule({
    providers: [
      ...appConfig.providers,
      ...(records.reportReads === true ? [provideHttpClientTesting()] : []),
      { provide: fleet.provide, useValue: fleet.useValue },
      {
        provide: SignalStrategyRegistryService,
        useValue: { entries: () => (entries instanceof Error ? Promise.reject(entries) : Promise.resolve(entries)) },
      },
      {
        provide: BacktestRunsService,
        useValue: {
          get: (id: number) => (records.runFailure ? throwError(() => records.runFailure) : of(records.runs?.[id] ?? null)),
        },
      },
      {
        provide: GridSearchService,
        useValue: {
          get: async (id: string) => {
            const search = records.searches?.[id];
            if (search === undefined) throw new HttpErrorResponse({ status: 404 });
            return search;
          },
        },
      },
    ],
  });
}

/** Where an address ends up. No page is drawn, so only the entry's own reads happen. */
async function land(url: string, records: Records = {}) {
  configure(records);
  const router = TestBed.inject(Router);
  await router.navigateByUrl(url);
  return router;
}

/** An address opened in a page, for the words it shows where it stays. Only the entry page is drawn. */
async function show(url: string, records: Records = {}) {
  configure(records);
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return TestBed.inject(Router);
}

describe('an old Strategy Lab address (T01)', () => {
  it.each([
    ['/strategy-lab?run=7&tab=history#trades', `/signal-strategies/${EMA}/explore/backtest?tab=history&run=7#trades`],
    ['/strategy-lab?restoreRun=7&symbol=QQQ', `/signal-strategies/${EMA}/explore/backtest?symbol=QQQ&run=7`],
    ['/strategy-lab/runs/7?symbol=QQQ', `/signal-strategies/${EMA}/explore/backtest?symbol=QQQ&run=7`],
    ['/engine/runs/7', `/signal-strategies/${EMA}/explore/backtest?run=7`],
    ['/engine?run=7#x', `/signal-strategies/${EMA}/explore/backtest?run=7#x`],
    ['/lean-engine?run=7', `/signal-strategies/${EMA}/explore/backtest?run=7`],
    ['/lean-lab?run=7', `/signal-strategies/${EMA}/explore/backtest?run=7`],
    ['/signal-strategies/evidence/run/7?probe=1', `/signal-strategies/${EMA}/explore/backtest?probe=1&run=7`],
  ])('resolves the run’s owner from the run itself: %s lands on %s', async (url, lands) => {
    const router = await land(url, { runs: { 7: savedRun({ id: 7 }) } });

    expect(router.url).toBe(lands);
  });

  it('lands a launch link on the strategy it names, keeping the rest of its query and its fragment', async () => {
    const router = await land(`/strategy-lab?strategy=${RSI}&symbol=SPY&engine=python#top`);

    expect(router.url).toBe(`/signal-strategies/${RSI}/explore/backtest?symbol=SPY&engine=python#top`);
  });

  it('opens a chooser, at the address, when it names no run and no strategy', async () => {
    const router = await show('/strategy-lab');

    expect(router.url).toBe('/strategy-lab');
    expect(await screen.findByRole('heading', { name: 'Choose a strategy to backtest' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'EMA Crossover Signal' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/backtest`);
    expect(screen.getByRole('link', { name: 'RSI Mean Reversion' }).getAttribute('href')).toBe(`/signal-strategies/${RSI}/explore/backtest`);
  });

  it('opens the chooser, saying so, for a strategy the registry does not list, and picks none', async () => {
    const router = await show('/strategy-lab?strategy=no_such_thing');

    expect(router.url).toBe('/strategy-lab?strategy=no_such_thing');
    expect(await screen.findByText(/"no_such_thing" is not a registered strategy/)).toBeTruthy();
  });

  it('ends a link that claims an unlisted owner’s run for a registered strategy at the run itself, not at that strategy', async () => {
    const router = await land(`/strategy-lab?run=9&strategy=${RSI}`, { runs: { 9: savedRun({ id: 9, strategyName: 'user_provided' }) } });

    expect(router.url).toBe('/signal-strategies/evidence/run/9');
  });

  it('refuses a run that does not exist, keeping its id', async () => {
    const router = await show('/engine/runs/404');

    expect(router.url).toBe('/engine/runs/404');
    expect(await screen.findByText('Saved run #404 was not found.')).toBeTruthy();
  });

  it('refuses a run the link claims for another strategy, and offers the run under its real owner', async () => {
    const router = await show(`/strategy-lab?run=7&strategy=${RSI}`, { runs: { 7: savedRun({ id: 7 }) } });

    expect(router.url).toBe(`/strategy-lab?run=7&strategy=${RSI}`);
    expect(await screen.findByText(`Saved run #7 belongs to ${EMA}, but the link asked for ${RSI}.`)).toBeTruthy();
    expect(screen.getByRole('link', { name: `Open run #7 under ${EMA}` }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/backtest?run=7`);
  });

  it.each([
    ['not a number', '/strategy-lab?run=abc', /"abc" is not a saved run's number/],
    ['zero', '/strategy-lab?run=0', /"0" is not a saved run's number/],
  ])('refuses a run id that is %s without asking the server for it', async (_name, url, words) => {
    await show(url);

    expect(await screen.findByText(words)).toBeTruthy();
  });

  it('says the run could not be read, rather than that it does not exist, when the read fails', async () => {
    await show('/strategy-lab?run=7', { runFailure: new Error('data plane down') });

    expect(await screen.findByText('Saved run #7 could not be read.')).toBeTruthy();
    expect(screen.getByText('data plane down')).toBeTruthy();
  });

  it('says the registry could not be read, and names no strategy, when it fails', async () => {
    const router = await show('/strategy-lab/runs/7', { entries: new Error('registry down'), runs: { 7: savedRun({ id: 7 }) } });

    expect(router.url).toBe('/strategy-lab/runs/7');
    expect(await screen.findByText('The strategy registry could not be read.')).toBeTruthy();
  });
});

describe('a saved run no registered strategy holds (I08)', () => {
  const orphan = (strategyName: string) => ({ 9: savedRun({ id: 9, strategyName }) });

  it.each([
    ['/strategy-lab/runs/9', '/signal-strategies/evidence/run/9'],
    ['/strategy-lab?run=9&tab=history#trades', '/signal-strategies/evidence/run/9?tab=history#trades'],
    ['/engine/runs/9?symbol=QQQ', '/signal-strategies/evidence/run/9?symbol=QQQ'],
    ['/lean-lab?restoreRun=9', '/signal-strategies/evidence/run/9'],
  ])('sends %s to the run itself in one redirect, keeping the rest of the link', async (url, lands) => {
    const router = await land(url, { runs: orphan('user_provided') });

    expect(router.url).toBe(lands);
  });

  it.each(['user_provided', 'spy_ema_crossover', 'ema_crossover_2_bps'])(
    'shows a run that stored %s by itself, read-only, naming the strategy it stored',
    async (stored) => {
      const router = await show('/signal-strategies/evidence/run/9', { runs: orphan(stored), reportReads: true });

      expect(router.url).toBe('/signal-strategies/evidence/run/9');
      expect(await screen.findByRole('heading', { name: 'Saved run #9' })).toBeTruthy();
      expect(screen.getByText(new RegExp(`Run #9 stored the strategy "${stored}", which the registry does not list`))).toBeTruthy();
      expect(screen.getByRole('link', { name: 'Back to Signal strategies' }).getAttribute('href')).toBe('/signal-strategies');
      // The report is the existing one, with nothing around it that runs, sweeps or approves.
      expect(document.querySelector('app-strategy-lab-run-stats')).not.toBeNull();
      expect(screen.queryByRole('button', { name: /Run validation/ })).toBeNull();
      expect(screen.queryByRole('link', { name: /Sweep|Walk-forward|golden/i })).toBeNull();
    },
  );

  it('opens a registered strategy’s run in that strategy’s Explore, not by itself', async () => {
    const router = await land('/signal-strategies/evidence/run/9', { runs: orphan(EMA) });

    expect(router.url).toBe(`/signal-strategies/${EMA}/explore/backtest?run=9`);
  });

  it('still refuses a run that does not exist, keeping its id, at the evidence address', async () => {
    const router = await show('/signal-strategies/evidence/run/404');

    expect(router.url).toBe('/signal-strategies/evidence/run/404');
    expect(await screen.findByText('Saved run #404 was not found.')).toBeTruthy();
  });
});

describe('an old Grid Search or Walk-Forward address (T01)', () => {
  it.each([
    [`/grid-search?strategy=${EMA}&probe=1#x`, `/signal-strategies/${EMA}/explore/sweep?probe=1#x`],
    [`/walk-forward?strategy=${RSI}`, `/signal-strategies/${RSI}/explore/walk-forward`],
    ['/grid-search?search=abc', `/signal-strategies/${EMA}/explore/sweep?search=abc`],
    ['/walk-forward?search=abc&probe=1#x', `/signal-strategies/${EMA}/explore/walk-forward?probe=1&search=abc#x`],
    ['/signal-strategies/evidence/search/abc', `/signal-strategies/${EMA}/explore/sweep?search=abc`],
  ])('resolves the strategy from the search or the link: %s lands on %s', async (url, lands) => {
    const router = await land(url, { searches: { abc: savedSearch({ id: 'abc' }) } });

    expect(router.url).toBe(lands);
  });

  it.each([
    ['/grid-search', 'Choose a strategy to sweep'],
    ['/walk-forward', 'Choose a strategy to walk-forward'],
  ])('opens a chooser for %s when it names no strategy', async (url, title) => {
    const router = await show(url);

    expect(router.url).toBe(url);
    expect(await screen.findByRole('heading', { name: title })).toBeTruthy();
  });

  it('shows in the chooser that the registry does not allow a sweep of a harness, and links the others', async () => {
    await show('/grid-search');

    const list = await screen.findByRole('region', { name: 'Strategies' });
    expect(within(list).getByRole('link', { name: 'EMA Crossover Signal' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/sweep`);
    expect(within(list).queryByRole('link', { name: 'Deployment Validation' })).toBeNull();
    expect(within(list).getByText('This strategy is an operational harness, not a production candidate.')).toBeTruthy();
  });

  it('keeps the address and says so when the search stored a strategy the registry does not list', async () => {
    const router = await show('/walk-forward?search=gone', { searches: { gone: savedSearch({ id: 'gone', strategy_key: 'retired_thing' }) } });

    expect(router.url).toBe('/walk-forward?search=gone');
    expect(await screen.findByText(/stored the strategy "retired_thing", which the registry does not list/)).toBeTruthy();
  });

  it('refuses a search that does not exist, keeping its id', async () => {
    const router = await show('/grid-search?search=nope');

    expect(router.url).toBe('/grid-search?search=nope');
    expect(await screen.findByText('Grid search nope was not found.')).toBeTruthy();
  });

  it('refuses a search the link claims for another strategy', async () => {
    await show(`/grid-search?search=abc&strategy=${RSI}`, { searches: { abc: savedSearch({ id: 'abc' }) } });

    expect(await screen.findByText(`Grid search abc belongs to ${EMA}, but the link asked for ${RSI}.`)).toBeTruthy();
  });
});
