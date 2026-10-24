import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, within } from '@testing-library/angular';
import { afterEach, describe, expect, it } from 'vitest';

import { appConfig } from '../../../app.config';
import { GoldenSearchService } from '../../../components/golden-search/golden-search.service';
import { studyDetail } from '../../../components/golden-search/testing/fixtures';
import type { StudyDetail } from '../../../components/golden-search/golden-search.types';
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import type { StrategyRegistryEntry } from '../../signal-strategy-evidence';
import { SignalStrategyRegistryService } from '../../signal-strategy-registry.service';
import { harnessEntry, registryEntry } from '../../testing/fixtures';

const EMA = 'ema_crossover_signal';
const RSI = 'rsi_mean_reversion';

interface Records {
  entries?: readonly StrategyRegistryEntry[] | Error;
  studies?: Record<string, StudyDetail>;
  studyFailure?: Error;
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
      { provide: fleet.provide, useValue: fleet.useValue },
      {
        provide: SignalStrategyRegistryService,
        useValue: { entries: () => (entries instanceof Error ? Promise.reject(entries) : Promise.resolve(entries)) },
      },
      {
        provide: GoldenSearchService,
        useValue: {
          get: async (id: string) => {
            if (records.studyFailure) throw records.studyFailure;
            const study = records.studies?.[id];
            if (study === undefined) throw new HttpErrorResponse({ status: 404 });
            return study;
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

/** An address opened in a page, for the words it shows where it stays. */
async function show(url: string, records: Records = {}) {
  configure(records);
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return TestBed.inject(Router);
}

const OWN_STUDY: Record<string, StudyDetail> = { 's-1': studyDetail('approved', { id: 's-1', strategy_key: EMA }) };

describe('an old Golden Search address (T01)', () => {
  it.each([
    ['/golden-search/s-1?step=compare&view=report#top', `/signal-strategies/${EMA}/golden/studies/s-1?step=compare&view=report#top`],
    ['/golden-search/s-1', `/signal-strategies/${EMA}/golden/studies/s-1`],
    ['/signal-strategies/evidence/study/s-1?probe=1', `/signal-strategies/${EMA}/golden/studies/s-1?probe=1`],
    // A revision names the study to revise; the plan opens under the strategy that ran it, still carrying the id.
    ['/golden-search?revise=s-1', `/signal-strategies/${EMA}/golden/studies?revise=s-1`],
    ['/golden-search?revise=s-1&tab=history#x', `/signal-strategies/${EMA}/golden/studies?revise=s-1&tab=history#x`],
  ])('resolves the study’s owner from the study itself: %s lands on %s', async (url, lands) => {
    const router = await land(url, { studies: OWN_STUDY });

    expect(router.url).toBe(lands);
  });

  it('lands a link on the strategy it names, keeping the rest of its query and its fragment', async () => {
    const router = await land(`/golden-search?strategy=${RSI}&tab=history#top`);

    expect(router.url).toBe(`/signal-strategies/${RSI}/golden/studies?tab=history#top`);
  });

  it('opens a chooser, at the address, when it names no study and no strategy', async () => {
    const router = await show('/golden-search?tab=history');

    expect(router.url).toBe('/golden-search?tab=history');
    expect(await screen.findByRole('heading', { name: 'Choose a strategy to open its Golden studies' })).toBeTruthy();
    const list = screen.getByRole('region', { name: 'Strategies' });
    expect(within(list).getByRole('link', { name: 'EMA Crossover Signal' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/studies`);
    // The registry rules Golden Search out for a harness, and the chooser says so instead of linking it.
    expect(within(list).queryByRole('link', { name: 'Deployment Validation' })).toBeNull();
    expect(within(list).getByText('This strategy is an operational harness, not a production candidate.')).toBeTruthy();
  });

  it('names a strategy the registry does not list, and picks none', async () => {
    const router = await show('/golden-search?strategy=no_such_thing');

    expect(router.url).toBe('/golden-search?strategy=no_such_thing');
    expect(await screen.findByText(/"no_such_thing" is not a registered strategy/)).toBeTruthy();
  });

  it('keeps the address and says so when the study stored a strategy the registry does not list', async () => {
    const router = await show('/golden-search/s-9', { studies: { 's-9': studyDetail('approved', { id: 's-9', strategy_key: 'retired_thing' }) } });

    expect(router.url).toBe('/golden-search/s-9');
    expect(await screen.findByText(/stored the strategy "retired_thing", which the registry does not list/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Back to Signal strategies' }).getAttribute('href')).toBe('/signal-strategies');
  });

  it('refuses a study that does not exist, keeping its id', async () => {
    const router = await show('/golden-search/nope');

    expect(router.url).toBe('/golden-search/nope');
    expect(await screen.findByText('Study nope was not found.')).toBeTruthy();
  });

  it('refuses a study the link claims for another strategy, and offers it under its real owner', async () => {
    await show(`/golden-search/s-1?strategy=${RSI}`, { studies: OWN_STUDY });

    expect(await screen.findByText(`Study s-1 ran for ${EMA}, but the link asked for ${RSI}.`)).toBeTruthy();
    expect(screen.getByRole('link', { name: `Open it under ${EMA}` }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/studies/s-1`);
  });

  it('says the study could not be read, rather than that it does not exist, when the read fails', async () => {
    await show('/golden-search/s-1', { studyFailure: new Error('data plane down') });

    expect(await screen.findByText('Golden search study s-1 could not be read.')).toBeTruthy();
    expect(screen.getByText('data plane down')).toBeTruthy();
  });

  it('says the registry could not be read, and names no strategy, when it fails', async () => {
    const router = await show('/golden-search/s-1', { entries: new Error('registry down'), studies: OWN_STUDY });

    expect(router.url).toBe('/golden-search/s-1');
    expect(await screen.findByText('The strategy registry could not be read.')).toBeTruthy();
  });
});

describe('an old Strategy Validation address (T01)', () => {
  it.each([
    // The page's proof pipeline and flag are the strategy's Proof.
    [`/strategy-validation?strategy=${EMA}`, `/signal-strategies/${EMA}/proof`],
    [`/strategy-validation?strategy=${EMA}&probe=1#flag`, `/signal-strategies/${EMA}/proof?probe=1#flag`],
    // Its embedded Golden Validation workbench is the configuration review: a subview or a case asks for it.
    [`/strategy-validation?strategy=${EMA}&view=configuration`, `/signal-strategies/${EMA}/golden/configuration`],
    [`/strategy-validation?strategy=${EMA}&view=golden&run=41`, `/signal-strategies/${EMA}/golden/configuration?run=41`],
    [`/strategy-validation?strategy=${RSI}&case=3`, `/signal-strategies/${RSI}/golden/configuration?case=3`],
  ])('goes by subview, straight to its home: %s lands on %s', async (url, lands) => {
    const router = await land(url);

    expect(router.url).toBe(lands);
  });

  it('drops the qc_backtest_id key nothing reads, and keeps every other key', async () => {
    const router = await land(`/strategy-validation?strategy=${EMA}&qc_backtest_id=abc&probe=1`);

    expect(router.url).toBe(`/signal-strategies/${EMA}/proof?probe=1`);
  });

  it('opens a chooser for the subview it was asked for, picking no strategy', async () => {
    const proof = await show('/strategy-validation');

    expect(proof.url).toBe('/strategy-validation');
    expect(await screen.findByRole('heading', { name: 'Choose a strategy to see its proof' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'EMA Crossover Signal' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/proof`);
    // A harness has a proof of its own, so it is offered.
    expect(screen.getByRole('link', { name: 'Deployment Validation' }).getAttribute('href')).toBe('/signal-strategies/deployment_validation/proof');
  });

  it('opens the configuration chooser for a case with no strategy', async () => {
    await show('/strategy-validation?case=3');

    expect(await screen.findByRole('heading', { name: 'Choose a strategy to review a configuration' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'EMA Crossover Signal' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/configuration`);
  });

  it('names a strategy the registry does not list instead of opening its page', async () => {
    const router = await show('/strategy-validation?strategy=no_such_thing');

    expect(router.url).toBe('/strategy-validation?strategy=no_such_thing');
    expect(await screen.findByText(/"no_such_thing" is not a registered strategy/)).toBeTruthy();
  });
});
