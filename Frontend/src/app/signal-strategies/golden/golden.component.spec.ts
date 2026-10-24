import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { defaults, emaCapability, exposure, preflight, studyDetail } from '../../components/golden-search/testing/fixtures';
import type { GoldenQualificationSummary } from '../../components/golden-search/golden-search.types';
import type { StudyDetail } from '../../components/golden-search/golden-search.types';
import { etDayEndMs, etMidnightMs } from '../../shared/date/et-midnight';
import { EMA, savedRun, savedSearch, sweepRequest, sweepableEma } from '../explore/testing/explore-fixtures';
import { provideExploreWorld, type ExploreWorld } from '../explore/testing/explore-world';
import { configurationReview, harnessEntry, qualification, registryEntry } from '../testing/fixtures';
import { emptyWorld, type SignalStrategyServices, type SignalStrategyWorld } from '../testing/signal-strategy-world';

const RSI = 'rsi_mean_reversion';

afterEach(() => {
  TestBed.resetTestingModule();
});

function world(overrides: Partial<SignalStrategyWorld> = {}): SignalStrategyWorld {
  return emptyWorld({
    entries: [registryEntry(), registryEntry({ strategy_key: RSI, display_name: 'RSI Mean Reversion' }), harnessEntry()],
    schemas: [sweepableEma()],
    capabilities: [emaCapability()],
    ...overrides,
  });
}

async function open(
  url: string,
  setup: { world?: SignalStrategyWorld; evidence?: Partial<ExploreWorld>; services?: Partial<SignalStrategyServices> } = {},
) {
  const explore = provideExploreWorld(setup.world ?? world(), { runs: {}, searches: {}, ...setup.evidence }, {}, setup.services);
  TestBed.configureTestingModule({ providers: explore.providers });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return { harness, calls: explore.calls, router: TestBed.inject(Router) };
}

/** The planning calls a golden search plan makes, answered as the server would for the fixture plan. */
function planServices(extra: Record<string, unknown> = {}) {
  const createStudy = vi.fn();
  const command = vi.fn();
  const services = {
    defaults: vi.fn(async () => defaults()),
    preflight: vi.fn(async () => preflight()),
    list: vi.fn(async () => []),
    get: vi.fn(async (id: string) => studyDetail('awaiting_validation', { id })),
    createStudy,
    command,
    ...extra,
  };
  return { golden: services, createStudy, command };
}

describe('Golden → Versions', () => {
  const versions = (): GoldenQualificationSummary[] => [
    qualification({ id: 'gq-spy-ready', symbol: 'SPY', status: 'ready', is_default: true, status_explanation: 'Ready for the running program.', created_at_ms: 3 }),
    qualification({ id: 'gq-spy-revoked', symbol: 'SPY', status: 'revoked', status_explanation: 'Revoked by the owner.', created_at_ms: 2 }),
    qualification({ id: 'gq-qqq-stale', symbol: 'QQQ', status: 'stale', status_explanation: 'The program changed since it was proved.', created_at_ms: 4 }),
    qualification({ id: 'gq-qqq-unknown', symbol: 'QQQ', status: 'unverifiable', status_explanation: 'Its proof could not be read.', created_at_ms: 1 }),
  ];

  const rowOf = (id: string): HTMLElement => screen.getByText(id, { exact: false }).closest('tr') as HTMLElement;

  it('shows ready, stale, revoked and unknown versions for two symbols, each under its own symbol with its own status (T11)', async () => {
    await open(`/signal-strategies/${EMA}/golden/versions`, { world: world({ qualifications: versions() }) });

    const spy = await screen.findByRole('heading', { name: /SPY/ });
    expect(spy.textContent).toMatch(/1 ready\s*·\s*1 revoked/);
    expect(screen.getByRole('heading', { name: /QQQ/ }).textContent).toMatch(/1 stale\s*·\s*1 unverifiable/);
    expect(within(rowOf('gq-spy-ready')).getByText('Ready')).toBeTruthy();
    expect(within(rowOf('gq-spy-revoked')).getByText('Revoked')).toBeTruthy();
    expect(within(rowOf('gq-qqq-stale')).getByText('Stale')).toBeTruthy();
    expect(within(rowOf('gq-qqq-unknown')).getByText('Unverifiable')).toBeTruthy();
    // The default belongs to one symbol; nothing sums the symbols into a strategy verdict.
    expect(screen.getAllByText('default for SPY')).toHaveLength(1);
    expect(screen.queryByText(/default for QQQ/)).toBeNull();
    expect(screen.queryByText(/approved for every symbol|strategy-wide/i)).toBeNull();
  });

  it('offers Use in Deploy only for a ready version, carrying the strategy and the exact version', async () => {
    await open(`/signal-strategies/${EMA}/golden/versions`, { world: world({ qualifications: versions() }) });
    await screen.findByRole('heading', { name: /SPY/ });

    const deploy = screen.getAllByRole('link', { name: 'Use in Deploy' });
    expect(deploy).toHaveLength(1);
    expect(deploy[0].getAttribute('href')).toBe(`/brokers/alpaca?strategy=${EMA}&golden_qualification=gq-spy-ready`);
    expect(within(rowOf('gq-spy-revoked')).queryByRole('link', { name: 'Use in Deploy' })).toBeNull();
    expect(within(rowOf('gq-qqq-stale')).queryByRole('link', { name: 'Use in Deploy' })).toBeNull();
    expect(within(rowOf('gq-qqq-unknown')).queryByRole('link', { name: 'Use in Deploy' })).toBeNull();
    expect(within(rowOf('gq-spy-revoked')).getByText(/cannot be deployed or restored/)).toBeTruthy();
    expect(within(rowOf('gq-qqq-unknown')).getByText(/not offered/)).toBeTruthy();
  });

  it('links each version to the study that made it and to the selected baseline run it was reviewed with', async () => {
    await open(`/signal-strategies/${EMA}/golden/versions`, { world: world({ qualifications: [qualification({ id: 'gq-1', study_id: 'study-77', golden_run_id: 12 })] }) });

    const row = (await screen.findByText('gq-1')).closest('tr') as HTMLElement;
    expect(within(row).getByRole('link', { name: /Study study-77/ }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/studies/study-77`);
    expect(within(row).getByRole('link', { name: 'selected baseline run #12' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/configuration?case=12`);
  });

  it('discloses the final-test exposure a version was approved under, and a research override beside it', async () => {
    const approved = qualification({
      id: 'gq-1',
      research: { exposure_state: 'previously_used', claim: 'exploratory', research_override: true },
    });
    await open(`/signal-strategies/${EMA}/golden/versions`, { world: world({ qualifications: [approved] }) });

    const row = (await screen.findByText('gq-1')).closest('tr') as HTMLElement;
    expect(within(row).getByText(/Final test: previously_used · exploratory/)).toBeTruthy();
    expect(within(row).getByText(/Approved despite weak research evidence/)).toBeTruthy();
  });

  it('re-proves a stale version from its retained evidence, shows it replaying, and reads the versions again', async () => {
    const reprove = vi.fn(async (_id: string, _key: string) => qualification());
    const current = world({ qualifications: versions() });
    await open(`/signal-strategies/${EMA}/golden/versions`, { world: current, services: { golden: { reproveQualification: reprove } } });
    await screen.findByRole('heading', { name: /QQQ/ });
    expect(within(rowOf('gq-spy-ready')).queryByRole('button')).toBeNull();

    current.qualifications = versions().map((version) => (version.id === 'gq-qqq-stale' ? { ...version, status: 'ready' as const } : version));
    fireEvent.click(within(rowOf('gq-qqq-stale')).getByRole('button', { name: 'Re-prove from retained evidence' }));

    await waitFor(() => expect(reprove).toHaveBeenCalledTimes(1));
    expect(reprove.mock.calls[0][0]).toBe('gq-qqq-stale');
    expect(reprove.mock.calls[0][1]).toMatch(/\S/);
    // The status shown is the server's, read again, not the page's guess.
    await waitFor(() => expect(within(rowOf('gq-qqq-stale')).getByText('Ready')).toBeTruthy());
  });

  it('says why a re-proof was refused beside that version only, and leaves its status as it was', async () => {
    const reprove = vi.fn(async () => {
      throw { error: { detail: { message: 'The recorded results no longer match the program.' } } };
    });
    await open(`/signal-strategies/${EMA}/golden/versions`, { world: world({ qualifications: versions() }), services: { golden: { reproveQualification: reprove } } });

    fireEvent.click(await screen.findByRole('button', { name: 'Re-prove from retained evidence' }));

    expect((await within(rowOf('gq-qqq-stale')).findByRole('alert')).textContent).toBe('The recorded results no longer match the program.');
    expect(within(rowOf('gq-qqq-stale')).getByText('Stale')).toBeTruthy();
    expect(within(rowOf('gq-spy-ready')).queryByRole('alert')).toBeNull();
  });

  it('says the versions are unknown when they cannot be read, never that there are none', async () => {
    await open(`/signal-strategies/${EMA}/golden/versions`, { world: world({ qualifications: new Error('research store down') }) });

    expect(await screen.findByText('Qualified versions could not be read.')).toBeTruthy();
    expect(screen.queryByText(/No symbol has a qualified version/)).toBeNull();
  });

  it('gives an operational harness the registry’s reason, since no version can exist for it', async () => {
    await open('/signal-strategies/deployment_validation/golden/versions', { world: world({ qualifications: [] }) });

    expect(await screen.findByText('This strategy is an operational harness, not a production candidate.')).toBeTruthy();
  });
});

describe('Golden → Configuration review', () => {
  it('reviews this strategy’s selected baseline runs only, and shows the registry corpus point apart from them (T11)', async () => {
    const list = vi.fn(() => of([configurationReview({ id: 5, label: 'SPY baseline' })]));
    await open(`/signal-strategies/${EMA}/golden/configuration`, { services: { goldenValidation: { list } } });

    expect(await screen.findByRole('heading', { name: 'Registry corpus point' })).toBeTruthy();
    expect(screen.getByText(/Validated for SPY:/).textContent).toContain('fast_period = 5 · slow_period = 10');
    expect(screen.getByText(/not a selected baseline run, not a Golden Search version/)).toBeTruthy();
    expect(await screen.findByRole('heading', { name: 'Selected baseline runs' })).toBeTruthy();
    expect(list).toHaveBeenCalledWith({ strategyName: EMA });
    expect(await screen.findByRole('navigation', { name: 'Selected baseline run catalog' })).toBeTruthy();
    expect(screen.queryByText('Validation Golden Run')).toBeNull();
  });

  it('opens the review a version names, by its own id', async () => {
    const list = vi.fn(() => of([configurationReview({ id: 5, label: 'First' }), configurationReview({ id: 6, label: 'Second', symbol: 'QQQ' })]));
    await open(`/signal-strategies/${EMA}/golden/configuration?case=6`, { services: { goldenValidation: { list } } });

    expect((await screen.findByRole('heading', { name: 'Second' })).closest('article')?.textContent).toContain('QQQ');
  });

  it('opens no review, and says so, when the version’s baseline run is not among the strategy’s listed reviews', async () => {
    const list = vi.fn(() => of([configurationReview({ id: 5, label: 'First' }), configurationReview({ id: 6, label: 'Second', symbol: 'QQQ' })]));
    await open(`/signal-strategies/${EMA}/golden/configuration?case=57`, { services: { goldenValidation: { list } } });

    expect(await screen.findByText(`Selected baseline run #57 is not among ${EMA}'s listed baseline runs. Nothing is selected.`)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'First' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Second' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save review' })).toBeNull();
  });

  it('selects one of this strategy’s own runs as the baseline run only with a note, and offers nothing for another strategy’s', async () => {
    const designate = vi.fn(() => of(configurationReview({ id: 9, source_run_id: 41 })));
    const mine = await open(`/signal-strategies/${EMA}/golden/configuration?run=41`, {
      evidence: { runs: { 41: savedRun({ id: 41 }), 42: savedRun({ id: 42, strategyName: RSI }) } },
      services: { goldenValidation: { list: () => of([]), designate } },
    });
    expect(await screen.findByRole('heading', { name: 'Select this Python run as the baseline run' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Select as baseline run' }));
    expect(await screen.findByText('Explain why this is the selected baseline run.')).toBeTruthy();
    expect(designate).not.toHaveBeenCalled();
    fireEvent.input(screen.getByLabelText(/Why is this the chosen baseline/), { target: { value: 'The run I trust for SPY.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Select as baseline run' }));
    await waitFor(() => expect(designate).toHaveBeenCalledTimes(1));
    expect(designate.mock.calls[0]).toEqual([expect.objectContaining({ source_run_id: 41, rationale: 'The run I trust for SPY.' })]);

    await mine.harness.navigateByUrl(`/signal-strategies/${EMA}/golden/configuration?run=42`);
    expect(await screen.findByText(`Saved run #42 belongs to ${RSI}, not ${EMA}.`)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Select this Python run as the baseline run' })).toBeNull();
    expect(screen.getByRole('link', { name: `Open run #42 under ${RSI}` }).getAttribute('href')).toBe(`/signal-strategies/${RSI}/golden/configuration?run=42`);
  });

  it.each([
    ['one that does not exist', '99', 'Saved run #99 was not found.'],
    ['one that is not a number', 'abc', '"abc" is not a saved run\'s number.'],
  ])('refuses %s in words, and offers no baseline for it', async (_name, run, words) => {
    await open(`/signal-strategies/${EMA}/golden/configuration?run=${run}`, { services: { goldenValidation: { list: () => of([]) } } });

    expect(await screen.findByText(words)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Select as baseline run' })).toBeNull();
  });

  it('keeps ADR 0061’s rule that unavailable or Python-only evidence advances only as an acknowledged Manual override, with its note', async () => {
    const review = vi.fn(() => of(configurationReview({ id: 5 })));
    const needsOverride = configurationReview({
      id: 5,
      latest_review: null,
      review_is_current: null,
      state: 'candidate',
      evidence_applicability: { status: 'unknown', explanation: 'The conventions of this run are not recorded.', requires_manual_override: true },
    });
    await open(`/signal-strategies/${EMA}/golden/configuration`, { services: { goldenValidation: { list: () => of([needsOverride]), review } } });
    await screen.findByRole('heading', { name: 'Record judgment' });

    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    expect(await screen.findByText('A review note is required for either acceptance or rejection.')).toBeTruthy();

    fireEvent.input(screen.getByLabelText(/Review note/), { target: { value: 'Python-only baseline; I accept it for Paper.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    expect(await screen.findByText(/Acknowledge Manual override/)).toBeTruthy();
    expect(review).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText(/Manual override: I accept the recorded affected or unknown conventions/));
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await waitFor(() => expect(review).toHaveBeenCalledTimes(1));
    expect(review.mock.calls[0]).toEqual([5, expect.objectContaining({ decision: 'accept', acknowledge_provenance_risk: true, reason: 'Python-only baseline; I accept it for Paper.' })]);
  });
});

describe('Golden → Studies', () => {
  it('opens on the strategy’s own plan: its strategy is fixed, and nothing is planned for another', async () => {
    const { golden } = planServices();
    await open(`/signal-strategies/${EMA}/golden`, { services: { golden } });

    expect(await screen.findByRole('tab', { name: /new study/i })).toBeTruthy();
    expect(screen.getByText(/Opening a study or a plan starts nothing/)).toBeTruthy();
    const setup = await screen.findByRole('region', { name: 'Strategy, instrument and starting point' });
    expect(within(setup).getByText('EMA Crossover Signal')).toBeTruthy();
    expect(within(setup).queryByRole('combobox', { name: 'Strategy' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Chart guide' }).getAttribute('href')).toBe('/docs/golden-search-guide');
  });

  it('lists only this strategy’s studies in History, with each one opening under it', async () => {
    const list = vi.fn(async () => [
      { ...studyDetail('approved', { id: 'study-0002-bbbb' }), consumed_evaluations: 10 },
    ]);
    const { golden } = planServices({ list });
    await open(`/signal-strategies/${EMA}/golden/studies?tab=history`, { services: { golden } });

    const link = await screen.findByRole('link', { name: /open study study-00/i });
    expect(link.getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/studies/study-0002-bbbb`);
    expect(list).toHaveBeenCalledWith(expect.objectContaining({ strategy_key: EMA }));
    expect(screen.queryByLabelText('Filter by strategy')).toBeNull();
  });

  it('says a golden search is not allowed for an operational harness, and offers no plan', async () => {
    const { golden } = planServices();
    await open('/signal-strategies/deployment_validation/golden/studies', { services: { golden } });

    expect(await screen.findByText('The registry does not allow a golden search of this strategy.')).toBeTruthy();
    expect(screen.getByText('This strategy is an operational harness, not a production candidate.')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Strategy, instrument and starting point' })).toBeNull();
  });

  it('says so when Golden Search declares nothing for the strategy, and does not guess a declaration', async () => {
    const { golden } = planServices();
    await open(`/signal-strategies/${RSI}/golden/studies`, { services: { golden } });

    expect(await screen.findByText('Golden Search declares nothing for rsi_mean_reversion.')).toBeTruthy();
  });

  it('locks a plan into a study only when the owner locks it, and then opens that study under the strategy', async () => {
    const created = studyDetail('locked', { id: 'study-0009-zzzz' });
    const { golden, createStudy } = planServices({ createStudy: vi.fn(async () => ({ study: created, jobId: null })) });
    const { router } = await open(`/signal-strategies/${EMA}/golden/studies`, { services: { golden } });
    await screen.findByRole('region', { name: 'Strategy, instrument and starting point' });
    expect(createStudy).not.toHaveBeenCalled();
    expect(router.url).toBe(`/signal-strategies/${EMA}/golden/studies`);
  });
});

describe('Explore → Golden (T12)', () => {
  /** The final test the fixture plan lays out: 2026-01-01 through 2026-03-31. */
  const OVERLAPPING = sweepRequest({ start_ms: etMidnightMs('2025-06-02'), end_ms: etDayEndMs('2026-02-27') });
  const EARLIER = sweepRequest({ start_ms: etMidnightMs('2024-02-01'), end_ms: etDayEndMs('2025-09-30') });

  async function planFrom(url: string, evidence: Partial<ExploreWorld>, golden = planServices()) {
    const opened = await open(url, { evidence, services: { golden: golden.golden } });
    await screen.findByRole('region', { name: 'Started from' });
    return { ...opened, ...golden };
  }

  it('opens an unlocked plan from a sweep: its ranges fill the plan, and nothing is locked, started or chosen for the owner', async () => {
    const { calls, createStudy, command, golden } = await planFrom(`/signal-strategies/${EMA}/golden/studies?search=grid-search-0001`, {
      searches: { 'grid-search-0001': savedSearch({ request: EARLIER }) },
    });

    const notice = screen.getByRole('region', { name: 'Started from' });
    expect(within(notice).getByRole('link', { name: 'sweep grid-sea' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/sweep?search=grid-search-0001`);
    expect(notice.textContent).toContain('Nothing is locked or running');
    expect(within(notice).getByText('Leader and cell results').closest('li')?.textContent).toContain('never starts from the sweep’s leader');
    await waitFor(() => expect((screen.getByLabelText('Fast EMA length low') as HTMLInputElement).value).toBe('5'));
    expect((screen.getByLabelText('Fast EMA length high') as HTMLInputElement).value).toBe('9');
    await screen.findByText(/plan is ready to lock/i);
    expect(golden.preflight).toHaveBeenCalled();
    expect(createStudy).not.toHaveBeenCalled();
    expect(command).not.toHaveBeenCalled();
    expect(calls.jobs.started).toEqual([]);
    // The final interval is the plan's own layout, not the explored window.
    expect(golden.defaults).toHaveBeenCalledWith(EMA, 'SPY');
  });

  it('never shows an interval Explore already viewed as unexposed, even when recorded research says it was not opened', async () => {
    await planFrom(`/signal-strategies/${EMA}/golden/studies?search=grid-search-0001`, {
      searches: { 'grid-search-0001': savedSearch({ request: OVERLAPPING }) },
    });

    const viewed = await screen.findByText(/Final-test dates: already viewed in Explore\./);
    expect(viewed.closest('p')?.textContent).toContain('sweep grid-sea covered 2025-06-02 to 2026-02-27');
    expect(viewed.closest('p')?.textContent).toContain('not an untouched look');
    // The server's own answer stays, labelled as what it is, without the claim of a fresh look.
    expect(await screen.findByText(/Recorded research: Not opened in recorded research\./)).toBeTruthy();
    expect(screen.queryByText(/Final-test dates: Not opened/)).toBeNull();
    expect(screen.queryByText(/fresh, confirmatory look/)).toBeNull();
  });

  it('keeps the server’s exposure answer when the explored window does not reach the final test', async () => {
    await planFrom(`/signal-strategies/${EMA}/golden/studies?search=grid-search-0001`, {
      searches: { 'grid-search-0001': savedSearch({ request: EARLIER }) },
    });

    expect(await screen.findByText(/Final-test dates: Not opened in recorded research\./)).toBeTruthy();
    expect(screen.queryByText(/already viewed in Explore/)).toBeNull();
  });

  it('shows history unknown when the server says an explored run overlaps, and does not soften it', async () => {
    const golden = planServices({
      preflight: vi.fn(async () => preflight({ exposure: exposure({ state: 'history_unknown', explanation: 'A Grid Search overlapped these dates.' }) })),
    });
    await planFrom(`/signal-strategies/${EMA}/golden/studies?search=grid-search-0001`, { searches: { 'grid-search-0001': savedSearch({ request: EARLIER }) } }, golden);

    expect(await screen.findByText(/Final-test dates: History unknown\./)).toBeTruthy();
    expect(screen.getByText(/only be exploratory/)).toBeTruthy();
  });

  it('opens a plan from a backtest with its symbol and costs, and carries no setting as a range', async () => {
    const { golden } = await planFrom(`/signal-strategies/${EMA}/golden/studies?run=91`, { runs: { 91: savedRun({ id: 91, symbol: 'SPY', commissionPerOrder: 0.35 }) } });

    const notice = screen.getByRole('region', { name: 'Started from' });
    expect(within(notice).getByRole('link', { name: 'backtest run #91' })).toBeTruthy();
    expect(within(notice).getByText('Settings').closest('li')?.textContent).toContain('carried neither as a range nor as a starting point');
    await waitFor(() => expect(golden.defaults).toHaveBeenCalledWith(EMA, 'QQQ'));
    expect(within(notice).getByText('Window').closest('li')?.textContent).toContain('2026-03-02 to 2026-04-02');
  });

  it('opens a plan from a walk-forward study with its grid and its fold lengths', async () => {
    const study = {
      id: 'wf-0001',
      strategy_key: EMA,
      symbol: 'SPY',
      status: 'completed',
      incomplete: false,
      fold_count: 4,
      completed_folds: 4,
      failed_folds: 0,
      request: { ...EARLIER, training_months: 9, test_months: 3 },
    } as unknown as NonNullable<ExploreWorld['walkForwardStudies']>[string];
    await planFrom(`/signal-strategies/${EMA}/golden/studies?walkForward=wf-0001`, { searches: {}, walkForwardStudies: { 'wf-0001': study } });

    const notice = screen.getByRole('region', { name: 'Started from' });
    expect(within(notice).getByRole('link', { name: 'walk-forward study wf-0001' })).toBeTruthy();
    expect(within(notice).getByText('Training window').closest('div')?.textContent).toContain('9 months');
    await waitFor(() => expect((screen.getByLabelText('Training window (months)') as HTMLInputElement).value).toBe('9'));
    expect((screen.getByLabelText('Test window (months)') as HTMLInputElement).value).toBe('3');
  });

  it('refuses a run another strategy produced, says so, and leaves the plan at Golden Search’s defaults', async () => {
    const { golden } = await (async () => {
      const setup = planServices();
      await open(`/signal-strategies/${EMA}/golden/studies?run=42`, {
        evidence: { runs: { 42: savedRun({ id: 42, strategyName: RSI }) } },
        services: { golden: setup.golden },
      });
      return setup;
    })();

    expect(await screen.findByText(`Saved run #42 belongs to ${RSI}, not ${EMA}.`)).toBeTruthy();
    await screen.findByRole('region', { name: 'Strategy, instrument and starting point' });
    expect(screen.queryByRole('region', { name: 'Started from' })).toBeNull();
    expect(golden.createStudy).not.toHaveBeenCalled();
  });

  it('lets the owner pick another symbol, which starts from that symbol’s defaults, not the handed-over ranges', async () => {
    const { golden } = await planFrom(`/signal-strategies/${EMA}/golden/studies?search=grid-search-0001`, {
      searches: { 'grid-search-0001': savedSearch({ request: EARLIER }) },
    });
    await waitFor(() => expect((screen.getByLabelText('Fast EMA length low') as HTMLInputElement).value).toBe('5'));
    expect(golden.defaults).toHaveBeenCalledTimes(1);
  });
});

describe('Golden → one study', () => {
  const OWN: StudyDetail = studyDetail('awaiting_validation', { id: 'study-0001-aaaa' });

  it('opens the study workbench under the strategy that ran it, and leads back to its studies', async () => {
    const { golden } = planServices({ get: vi.fn(async () => OWN) });
    await open(`/signal-strategies/${EMA}/golden/studies/study-0001-aaaa`, { services: { golden } });

    expect(await screen.findByRole('navigation', { name: 'Research steps' })).toBeTruthy();
    expect(screen.getByRole('link', { name: /All studies/ }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/studies`);
    // The study says what it is itself; the strip's one-line purpose is for the pages that list and plan.
    expect(screen.queryByText(/Opening a study or a plan starts nothing/)).toBeNull();
    expect(screen.getByRole('link', { name: 'How this works' }).getAttribute('href')).toBe('/docs/golden-search-manual?study=study-0001-aaaa');
  });

  it('refuses a study another strategy ran, and offers it under its real owner', async () => {
    const { golden } = planServices({ get: vi.fn(async () => OWN) });
    await open(`/signal-strategies/${RSI}/golden/studies/study-0001-aaaa`, { services: { golden } });

    expect(await screen.findByText(`Study study-00 ran for ${EMA}, not ${RSI}.`)).toBeTruthy();
    expect(screen.getByRole('link', { name: `Open it under ${EMA}` }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/golden/studies/study-0001-aaaa`);
    expect(screen.queryByRole('navigation', { name: 'Research steps' })).toBeNull();
  });

  it('keeps a failed proof non-overridable: the only way on is Retry qualification, and nothing approves it', async () => {
    const failed = studyDetail('qualification_failed', { id: 'study-0001-aaaa' });
    const { golden } = planServices({ get: vi.fn(async () => failed) });
    await open(`/signal-strategies/${EMA}/golden/studies/study-0001-aaaa?step=decision`, { services: { golden } });

    expect(await screen.findByRole('button', { name: 'Retry qualification' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /as golden/i })).toBeNull();
    expect(screen.queryByRole('link', { name: /use in deploy/i })).toBeNull();
  });
});
