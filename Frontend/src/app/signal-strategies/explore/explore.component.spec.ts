import { Router } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { defaults, emaCapability, preflight } from '../../components/golden-search/testing/fixtures';
import type { JobState } from '../../services/jobs.service';
import { harnessEntry, registryEntry } from '../testing/fixtures';
import { emptyWorld } from '../testing/signal-strategy-world';
import { EMA, savedRun, savedSearch, sweepableEma } from './testing/explore-fixtures';
import { provideExploreWorld, type ExploreCalls, type ExploreWorld } from './testing/explore-world';

const RSI = 'rsi_mean_reversion';

beforeEach(() => sessionStorage.clear());

afterEach(() => {
  TestBed.resetTestingModule();
});

async function open(url: string, evidence: Partial<ExploreWorld> = {}) {
  const { providers, calls } = provideExploreWorld(
    emptyWorld({
      entries: [registryEntry(), registryEntry({ strategy_key: RSI, display_name: 'RSI Mean Reversion' })],
      schemas: [sweepableEma(), { ...sweepableEma(), name: RSI, display_name: 'RSI Mean Reversion' }],
    }),
    { runs: {}, searches: {}, ...evidence },
  );
  TestBed.configureTestingModule({ providers });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return { harness, calls, router: TestBed.inject(Router) };
}

/** The run control of the open Backtest, once its strategy and window are in. */
async function runButton(): Promise<HTMLButtonElement> {
  const button = await screen.findByRole('button', { name: /Run validation|Running…/ });
  return button as HTMLButtonElement;
}

async function startRun(calls: ExploreCalls): Promise<void> {
  const button = await runButton();
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
  await waitFor(() => expect(calls.jobs.started).toHaveLength(1));
}

function job(status: JobState['status']): JobState {
  return { id: 'job-1', type: 'engine_backtest', status, phase: 'running_indicators', recentLogs: [], logSeq: 0 };
}

describe('a strategy’s Explore', () => {
  it('opens on Backtest and links its three tools, with Sweep and Walk-forward greyed and explained where the registry rules them out', async () => {
    const { providers } = provideExploreWorld(emptyWorld({ entries: [harnessEntry()], schemas: [] }));
    TestBed.configureTestingModule({ providers });
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl('/signal-strategies/deployment_validation/explore');

    const strip = await screen.findByRole('navigation', { name: 'Explore tools' });
    expect(TestBed.inject(Router).url).toBe('/signal-strategies/deployment_validation/explore/backtest');
    expect(within(strip).getByRole('link', { name: 'Backtest' }).getAttribute('aria-current')).toBe('page');
    for (const tool of ['Sweep', 'Walk-forward']) {
      expect(within(strip).queryByRole('link', { name: tool })).toBeNull();
      expect(within(strip).getByText(tool).getAttribute('aria-disabled')).toBe('true');
    }
    expect(screen.getByText('Sweep', { selector: 'strong' }).closest('li')?.textContent).toContain(
      'This strategy is an operational harness, not a production candidate.',
    );
  });

  it('says what Explore is for and that nothing in it approves settings', async () => {
    await open(`/signal-strategies/${EMA}/explore/sweep`);

    expect(await screen.findByText(/nothing in Explore approves settings/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'How this works' }).getAttribute('href')).toBe('/strategy-lab/docs');
    expect(screen.getByRole('link', { name: 'Strategy reference' }).getAttribute('href')).toBe('/strategy-docs');
  });

  it('keeps a running backtest across a switch to Sweep and back: the same run, one job, one runner', async () => {
    const { harness, calls } = await open(`/signal-strategies/${EMA}/explore/backtest`);
    await startRun(calls);
    calls.jobs.active.set([job('running')]);

    await harness.navigateByUrl(`/signal-strategies/${EMA}/explore/sweep`);
    await screen.findByRole('tab', { name: /new search/i });
    // While another tool is open the strip still says the backtest is going.
    expect(within(screen.getByRole('navigation', { name: 'Explore tools' })).getByRole('link', { name: /Backtest/ }).textContent).toContain('running');

    await harness.navigateByUrl(`/signal-strategies/${EMA}/explore/backtest`);
    const button = await runButton();

    expect(button.textContent).toContain('Running…');
    expect(button.disabled).toBe(true);
    expect(calls.jobs.started).toHaveLength(1);
    // Each runner asks the job registry once when it is built: there is one.
    expect(calls.jobs.refreshCalls).toBe(1);
  });

  it('does not pull the owner back to Backtest when the run finishes elsewhere, and leads back to it', async () => {
    const { harness, calls, router } = await open(`/signal-strategies/${EMA}/explore/backtest`, { runs: { 91: savedRun() } });
    await startRun(calls);
    calls.jobs.active.set([job('running')]);
    await harness.navigateByUrl(`/signal-strategies/${EMA}/explore/sweep`);
    await screen.findByRole('tab', { name: /new search/i });

    calls.jobs.result = { success: true, total_trades: 3, net_profit: 120, study_id: 91, save_outcome: 'saved' };
    calls.jobs.active.set([job('completed')]);

    const strip = screen.getByRole('navigation', { name: 'Explore tools' });
    await waitFor(() => expect(within(strip).getByRole('link', { name: /Backtest/ }).textContent).toContain('run ready'));
    expect(router.url).toBe(`/signal-strategies/${EMA}/explore/sweep`);
    expect(within(strip).getByRole('link', { name: /Backtest/ }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/backtest?run=91`);

    fireEvent.click(within(strip).getByRole('link', { name: /Backtest/ }));
    await waitFor(() => expect(router.url).toBe(`/signal-strategies/${EMA}/explore/backtest?run=91`));
  });

  it('leads the Backtest tab back to the run it last showed', async () => {
    const { harness } = await open(`/signal-strategies/${EMA}/explore/backtest?run=91`, { runs: { 91: savedRun() } });
    // The loaded run collapses the rail; the tool is open once its tabs are.
    await screen.findByRole('tab', { name: /Workbench/ });

    await harness.navigateByUrl(`/signal-strategies/${EMA}/explore/walk-forward`);
    const strip = await screen.findByRole('navigation', { name: 'Explore tools' });

    await waitFor(() =>
      expect(within(strip).getByRole('link', { name: 'Backtest' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/backtest?run=91`),
    );
  });

  it('walks a run into a sweep and a sweep into a walk-forward by their own links, filling each form and starting nothing (T12)', async () => {
    const { harness, calls, router } = await open(`/signal-strategies/${EMA}/explore/backtest?run=91`, {
      runs: { 91: savedRun() },
      searches: { 'grid-search-0001': savedSearch() },
    });

    fireEvent.click(await screen.findByRole('link', { name: "Sweep this run's settings" }));
    const fromRun = await screen.findByRole('region', { name: 'Started from' });
    expect(router.url).toBe(`/signal-strategies/${EMA}/explore/sweep?run=91`);
    expect(within(fromRun).getByRole('link', { name: 'backtest run #91' })).toBeTruthy();

    // The sweep the owner launched from it is opened from History; its result leads on.
    await harness.navigateByUrl(`/signal-strategies/${EMA}/explore/sweep?search=grid-search-0001`);
    fireEvent.click(await screen.findByRole('link', { name: /Test this grid out of sample in Walk-forward/ }));
    const fromSweep = await screen.findByRole('region', { name: 'Started from' });

    expect(router.url).toBe(`/signal-strategies/${EMA}/explore/walk-forward?search=grid-search-0001`);
    expect(within(fromSweep).getByRole('link', { name: /^sweep / }).getAttribute('href')).toBe(
      `/signal-strategies/${EMA}/explore/sweep?search=grid-search-0001`,
    );
    expect(within(fromSweep).getByText('Training and test lengths')).toBeTruthy();
    expect(calls.jobs.started).toEqual([]);
  });

  it('walks a run and a sweep into a golden plan by their own links: unlocked, the explored window flagged, nothing started (T12)', async () => {
    const createStudy = vi.fn();
    const command = vi.fn();
    const golden = { defaults: vi.fn(async () => defaults()), preflight: vi.fn(async () => preflight()), list: vi.fn(async () => []), createStudy, command };
    const { providers, calls } = provideExploreWorld(
      emptyWorld({ entries: [registryEntry()], schemas: [sweepableEma()], capabilities: [emaCapability()] }),
      { runs: { 91: savedRun({ symbol: 'SPY' }) }, searches: { 'grid-search-0001': savedSearch() } },
      {},
      { golden },
    );
    TestBed.configureTestingModule({ providers });
    const harness = await RouterTestingHarness.create();
    const router = TestBed.inject(Router);

    await harness.navigateByUrl(`/signal-strategies/${EMA}/explore/backtest?run=91`);
    fireEvent.click(await screen.findByRole('link', { name: 'Plan a golden search from this run' }));
    const fromRun = await screen.findByRole('region', { name: 'Started from' });
    expect(router.url).toBe(`/signal-strategies/${EMA}/golden/studies?run=91`);
    expect(within(fromRun).getByRole('link', { name: 'backtest run #91' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/backtest?run=91`);

    await harness.navigateByUrl(`/signal-strategies/${EMA}/explore/sweep?search=grid-search-0001`);
    fireEvent.click(await screen.findByRole('link', { name: /Plan a golden search over these ranges/ }));
    const fromSweep = await screen.findByRole('region', { name: 'Started from' });

    expect(router.url).toBe(`/signal-strategies/${EMA}/golden/studies?search=grid-search-0001`);
    expect(within(fromSweep).getByRole('link', { name: /^sweep / }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore/sweep?search=grid-search-0001`);
    // The sweep viewed 2026-01-02 to 2026-06-30, which reaches the plan's final test: the plan says so and does not call those dates unexposed.
    expect(await screen.findByText(/Final-test dates: already viewed in Explore\./)).toBeTruthy();
    expect(screen.queryByText(/Final-test dates: Not opened/)).toBeNull();
    expect(createStudy).not.toHaveBeenCalled();
    expect(command).not.toHaveBeenCalled();
    expect(calls.jobs.started).toEqual([]);
  });

  it('shows Define the values the workbench is trying out, as exploratory and against their defaults', async () => {
    const { harness } = await open(`/signal-strategies/${EMA}/explore/backtest`);
    await runButton();
    const field = screen.getAllByLabelText(/^Fast Period/)[0] as HTMLInputElement;
    fireEvent.input(field, { target: { value: '8' } });

    await harness.navigateByUrl(`/signal-strategies/${EMA}/define`);

    const section = (await screen.findByRole('heading', { name: 'Current exploratory values' })).closest('section') as HTMLElement;
    const row = (await within(section).findByText('fast_period')).closest('tr') as HTMLElement;
    expect(within(row).getByText('8')).toBeTruthy();
    expect(within(row).getByText('changed')).toBeTruthy();
    expect(within(section).getAllByText('the default', { selector: 'td' }).length).toBeGreaterThan(0);
    // Exploratory values are never offered for approval from here.
    expect(within(section).queryByRole('button')).toBeNull();
  });

  it('starts another strategy’s Explore afresh: its backtest is not the one running for the last strategy', async () => {
    const { harness, calls } = await open(`/signal-strategies/${EMA}/explore/backtest`);
    await startRun(calls);
    calls.jobs.active.set([job('running')]);

    await harness.navigateByUrl(`/signal-strategies/${RSI}/explore/backtest`);
    await waitFor(() => expect(calls.jobs.refreshCalls).toBe(2));

    // The registry shows a job in flight to the new runner, which can only wait; it did not start it.
    expect(calls.jobs.started).toHaveLength(1);
    expect(screen.getAllByText('RSI Mean Reversion').length).toBeGreaterThan(0);
  });
});
