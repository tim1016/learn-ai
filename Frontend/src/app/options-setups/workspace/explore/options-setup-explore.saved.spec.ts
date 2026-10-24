import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { exampleBody, servedExamples, servedSaved } from '../../../components/options-backtest/testing/fixtures';
import { pickDates } from '../../../components/options-backtest/testing/page-actions';
import { runDetail, runItem } from '../../../components/options-backtest/testing/run-fixtures';
import { scriptedRun } from '../../../components/options-backtest/testing/run-script';
import { RUN_WORKSPACE } from '../../../components/options-evidence/testing';
import { editSlippage, goToDefine, goToExplore, libraryWorld, saveInDefine } from '../../testing/explore-actions';
import { indexOf, runOf, savedName } from '../../testing/fixtures';
import { renderExplore, setupUrl } from '../../testing/render-workspace';

const startButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
const url = () => decodeURIComponent(TestBed.inject(Router).url);
const go = (target: string) => TestBed.inject(Router).navigateByUrl(target);
const viewLink = (name: string) => within(screen.getByRole('navigation', { name: 'Explore views' })).getByRole('link', { name });

/** What the library holds when a spec needs nothing saved but "Mine". */
const mine = () => ({ list: vi.fn(async () => [...servedExamples(), servedSaved('Mine', exampleBody())]) });

/** A server that keeps what is saved, runs a backtest as a job, and holds one saved setup, "Mine", as the shipped example. */
function savedWorld() {
  const setup = scriptedRun(vi);
  const world = libraryWorld(vi);
  world.library.set('Mine', exampleBody());
  setup.service.list.mockImplementation(world.service.list);
  setup.service.save.mockImplementation(world.service.save);
  return setup;
}

async function readyToStart(setup = savedWorld(), options: Parameters<typeof renderExplore>[2] = {}) {
  await renderExplore(setup.service, 'Mine', { jobs: setup.jobs, ...options });
  pickDates('2026-09-14', '2026-09-18');
  await waitFor(() => expect(startButton().disabled).toBe(false));
  return setup;
}

describe('a backtest of the saved setup', () => {
  it('tests the setup as saved while Define holds edits that are not saved, and says that those edits are not in it', async () => {
    const setup = savedWorld();
    await renderExplore(setup.service, 'Mine', { jobs: setup.jobs });
    await goToDefine();
    editSlippage('0.4');
    await goToExplore();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    expect(screen.getByText(/Define holds changes that are not saved\. They are not in this backtest/)).toBeTruthy();
    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    const request = setup.service.startRun.mock.calls[0]?.[0];
    expect(request?.setup.slippage_percent).toBe('0.3');
    expect(request?.setup_name).toBe('Mine');
  });

  it('tests what was saved once the edit is saved, and is not ready to start until the server has read that back', async () => {
    const setup = savedWorld();
    await renderExplore(setup.service, 'Mine', { jobs: setup.jobs });
    await goToDefine();
    editSlippage('0.4');
    await saveInDefine('Mine');
    await goToExplore();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    expect(screen.queryByText(/Define holds changes that are not saved/)).toBeNull();
    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    expect(setup.service.startRun.mock.calls[0]?.[0].setup.slippage_percent).toBe('0.4');
  });

  it('has nothing to test for a draft with no saved name: it says so, offers Define, and starts nothing', async () => {
    const setup = savedWorld();
    await renderExplore(setup.service, null, { jobs: setup.jobs });

    expect(await screen.findByText('This draft has no saved name, so there is no saved setup to test.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Start backtest' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Go to Define' }).getAttribute('href')).toBe('/options-setups/_new/define');
    expect(setup.service.startRun).not.toHaveBeenCalled();
  });

  it('keeps the runs reachable for a draft with no saved name: History lists them, and the views about one run say which run they need', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [runItem({ id: 30, job_id: 'job-30', status: 'complete' })]) });
    await renderExplore(service, null, { query: '?view=history' });

    expect(await screen.findByRole('button', { name: 'Open run 30' })).toBeTruthy();
  });
});

describe('editing Define after a run', () => {
  const ran = () => ({ ...exampleBody(), underlying: 'AAPL', slippage_percent: '0.45' });
  const COMPLETE = runItem({ id: 30, job_id: 'job-30', status: 'complete', setup_name: 'Mine' });

  it('does not change the setup the run shows: the run is read from its own record, before and after the setup is edited and saved', async () => {
    const setup = savedWorld();
    setup.service.runs.mockResolvedValue([COMPLETE]);
    setup.service.run.mockImplementation(async () => runDetail({ ...COMPLETE, setup: ran() }));
    await renderExplore(setup.service, 'Mine', { jobs: setup.jobs, query: '?view=history&run=30' });
    expect(await screen.findByText('Window for AAPL.')).toBeTruthy();

    await goToDefine();
    editSlippage('0.4');
    await saveInDefine('Mine');
    await go(`${setupUrl('Mine', 'explore')}?view=history&run=30`);

    expect(await screen.findByText('Window for AAPL.')).toBeTruthy();
    expect(screen.queryByText('Window for SPY.')).toBeNull();
    // Every setup the run's own record was put into words from is the run's: nothing of the edited or saved setup.
    const described = setup.service.describe.mock.calls.map(([body]) => body as { underlying: string; slippage_percent: string });
    expect(described.filter((body) => body.underlying === 'AAPL').every((body) => body.slippage_percent === '0.45')).toBe(true);
    expect(setup.service.run).toHaveBeenCalledWith(30);
  });

  it('does not change the setup a finished run of the Backtest view shows', async () => {
    const setup = await readyToStart();
    setup.service.run.mockResolvedValue(runDetail({ setup: { ...exampleBody(), slippage_percent: '0.3' } }));
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });
    setup.jobs.emit('job-1', { type: 'job.completed', result_url: '/x' });
    await screen.findByRole('region', { name: 'Headline figures' });

    await goToDefine();
    editSlippage('0.4');
    await saveInDefine('Mine');
    await goToExplore();

    // The saved setup is now the edited one, and a new backtest would test it; the run on screen is still the run that was made.
    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
    expect(setup.service.startRun.mock.calls[0]?.[0].setup.slippage_percent).toBe('0.3');
  });
});

describe('Take to Golden', () => {
  const index = (runHash: string) => indexOf({ runs: [runOf(7, runHash)] });

  async function afterARun(options: Parameters<typeof renderExplore>[2]) {
    // "Mine" holds the content `1`, the content the run below froze unless a spec says it froze another.
    options = { saved: [savedName('Mine', '1')], ...options };
    const setup = savedWorld();
    setup.service.run.mockResolvedValueOnce(runDetail({ status: 'running', finished_at_ms: null, figures: null, slippage: [], slippage_tolerance: null, headline: null })).mockResolvedValue(runDetail());
    await readyToStart(setup, options);
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });
    setup.jobs.emit('job-1', { type: 'job.completed', result_url: '/x' });
    await screen.findByRole('region', { name: 'Headline figures' });
    return setup;
  }

  it('carries the exact tested run to this setup’s Golden, and approves nothing', async () => {
    await afterARun({ index: index('1') });

    const link = await screen.findByRole('link', { name: 'Take to Golden' });

    expect(link.getAttribute('href')).toBe('/options-setups/Mine/golden?run=7');
    expect(screen.queryByRole('button', { name: /Approve golden setup|Review golden setup|Revoke golden setup/ })).toBeNull();
    expect(document.querySelector('app-golden-option-setup-approval')).toBeNull();
  });

  it('is not offered for a run that froze other content than the setup’s saved content, and says so', async () => {
    await afterARun({ index: index('2') });

    expect(await screen.findByText(/froze other content than this setup’s saved content/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Take to Golden' })).toBeNull();
  });

  it('is not offered while it cannot be read which content the run froze, and says so', async () => {
    const unread = async () => {
      throw new Error('store down');
    };
    await afterARun({ index: unread });

    expect(await screen.findByText(/needs to know which content the run froze/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Take to Golden' })).toBeNull();
  });

  it('stops being offered for a run once the setup is saved as other content: the offer follows the saved setups, not what Explore read when it opened', async () => {
    const holds = { digit: '1' };
    await afterARun({ index: index('1'), saved: () => [savedName('Mine', holds.digit)] });
    expect(await screen.findByRole('link', { name: 'Take to Golden' })).toBeTruthy();

    await goToDefine();
    editSlippage('0.4');
    // The save lands, and the saved setups read after it list "Mine" as the edited content.
    holds.digit = '2';
    await saveInDefine('Mine');
    await goToExplore();

    expect(await screen.findByText(/froze other content than this setup’s saved content/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Take to Golden' })).toBeNull();
  });

  it('is offered again once what the run froze is read: the evidence is read again when a run ends', async () => {
    let reads = 0;
    const setup = await afterARun({
      index: async () => {
        reads += 1;
        // Read at the start, before the run existed, then again when it ended.
        return indexOf({ runs: reads === 1 ? [] : [runOf(7, '1')] });
      },
    });

    expect(await screen.findByRole('link', { name: 'Take to Golden' })).toBeTruthy();
    expect(reads).toBeGreaterThan(1);
    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
  });
});

describe('an opened run has an address', () => {
  const COMPLETE = runItem({ id: 30, job_id: 'job-30', status: 'complete', setup_name: 'Straddle' });
  const world = () => fakeService(vi, { ...mine(), runs: vi.fn(async () => [COMPLETE, runItem({ id: 31, job_id: 'job-31', status: 'complete' })]), run: vi.fn(async (id: number) => runDetail({ ...COMPLETE, id })) });

  it('opening a run in History puts it in the address, and closing it takes it out again', async () => {
    await renderExplore(world(), 'Mine', { query: '?view=history' });

    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByRole('region', { name: 'Backtest run' });

    await waitFor(() => expect(url()).toBe('/options-setups/Mine/explore?view=history&run=30'));
    fireEvent.click(screen.getByRole('button', { name: 'Back to the runs' }));
    await screen.findByRole('button', { name: 'Open run 30' });
    await waitFor(() => expect(url()).toBe('/options-setups/Mine/explore?view=history'));
  });

  it('opens at its address with nothing else done, and from there goes on to the run’s performance', async () => {
    await renderExplore(world(), 'Mine', { query: '?view=history&run=31' });

    const panel = await screen.findByRole('region', { name: 'Backtest run' });

    expect(within(panel).getByRole('link', { name: 'Performance and research lineage' }).getAttribute('href')).toBe('/options-setups/Mine/explore?view=performance&run=31');
  });

  it('opens a run the list does not hold, by reading it by its id', async () => {
    const service = fakeService(vi, { ...mine(), runs: vi.fn(async () => []), run: vi.fn(async (id: number) => runDetail({ ...COMPLETE, id })) });
    await renderExplore(service, 'Mine', { query: '?view=history&run=99' });

    await screen.findByRole('region', { name: 'Backtest run' });

    expect(service.run).toHaveBeenCalledWith(99);
  });

  it('says in place that the run could not be read, and offers the way back to the list', async () => {
    const service = fakeService(vi, { ...mine(), runs: vi.fn(async () => [COMPLETE]) });
    await renderExplore(service, 'Mine', { query: '?view=history&run=99' });

    expect((await screen.findByRole('alert')).textContent).toContain('No run 99.');
    fireEvent.click(screen.getByRole('button', { name: 'Back to the runs' }));

    await screen.findByRole('button', { name: 'Open run 30' });
    await waitFor(() => expect(url()).toBe('/options-setups/Mine/explore?view=history'));
  });

  it('puts two runs side by side in the address, and shows them again from it', async () => {
    await renderExplore(world(), 'Mine', { query: '?view=history' });
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Compare run 30' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Compare run 31' }));

    fireEvent.click(screen.getByRole('button', { name: 'Compare' }));
    await screen.findByRole('button', { name: 'Back to the runs' });

    await waitFor(() => expect(url()).toBe('/options-setups/Mine/explore?view=history&run=30&comparator_run_id=31'));
    await go(`${setupUrl('Mine', 'explore')}?view=history`);
    await screen.findByRole('button', { name: 'Open run 30' });
    await go(`${setupUrl('Mine', 'explore')}?view=history&run=31&comparator_run_id=30`);
    expect(await screen.findByRole('button', { name: 'Back to the runs' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Open run 30' })).toBeNull();
  });
});

describe('the views about one run', () => {
  const evidence = () => ({ read: vi.fn(async () => RUN_WORKSPACE) });

  it('does not offer Take to Golden for a run of the saved content that did not complete, and says why', async () => {
    const service = fakeService(vi, { ...mine(), run: vi.fn(async () => runDetail({ id: 76 })) });
    await renderExplore(service, 'Mine', {
      evidence: evidence(),
      saved: [savedName('Mine', '1')],
      index: indexOf({ runs: [runOf(76, '1', { status: 'failed' })] }),
      query: '?view=performance&run=76',
    });

    expect(await screen.findByText(/Run 76 is failed, not complete, so it is not taken to Golden/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Take to Golden' })).toBeNull();
  });

  it('offers Take to Golden for a completed run of the saved content, carrying only the run', async () => {
    const service = fakeService(vi, { ...mine(), run: vi.fn(async () => runDetail({ id: 76 })) });
    await renderExplore(service, 'Mine', {
      evidence: evidence(),
      saved: [savedName('Mine', '1')],
      index: indexOf({ runs: [runOf(76, '1')] }),
      query: '?view=performance&run=76',
    });

    const link = await screen.findByRole('link', { name: 'Take to Golden' });

    expect(link.getAttribute('href')).toBe('/options-setups/Mine/golden?run=76');
  });

  it('reads the evidence index once for the setup, and says which saved setups hold the run’s content from the saved setups read', async () => {
    const service = fakeService(vi, { ...mine(), run: vi.fn(async () => runDetail({ id: 76 })) });
    const read = vi.fn(async () => indexOf({ runs: [runOf(76, '1')] }));
    await renderExplore(service, 'Mine', {
      evidence: evidence(),
      saved: [savedName('Mine', '0', { setup_sha256: RUN_WORKSPACE.setup_sha256 })],
      index: read,
      query: '?view=lineage&run=76',
    });

    expect(await screen.findByText(/Saved setups that hold this exact content today/)).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(1);
  });

  it('read that run’s retained evidence by its id, each at its own address, with one way between the views', async () => {
    const service = fakeService(vi, { ...mine(), run: vi.fn(async () => runDetail({ id: 76 })) });
    const reader = evidence();
    await renderExplore(service, 'Mine', { evidence: reader, saved: [savedName('Mine', '1')], index: indexOf({}), query: '?view=lineage&run=76' });

    expect(await screen.findByRole('heading', { name: RUN_WORKSPACE.headline })).toBeTruthy();
    expect(reader.read).toHaveBeenCalledWith('run', '76', undefined);
    expect(screen.getByRole('heading', { name: 'Approved from backtest' })).toBeTruthy();
    // The view carries no second set of links to the three views: Explore's own are the only ones.
    expect(screen.getAllByRole('link', { name: 'Performance' })).toHaveLength(1);
    expect(viewLink('Performance').getAttribute('href')).toBe('/options-setups/Mine/explore?view=performance&run=76');
    expect(viewLink('Historical comparison').getAttribute('href')).toBe('/options-setups/Mine/explore?view=comparison&run=76');
    expect(viewLink('Backtest').getAttribute('href')).toBe('/options-setups/Mine/explore');
    expect(viewLink('History').getAttribute('href')).toBe('/options-setups/Mine/explore?view=history');
  });

  it('shows the run’s performance from its address, and keeps the run in view when another view is left for', async () => {
    const service = fakeService(vi, { ...mine(), run: vi.fn(async () => runDetail({ id: 76 })) });
    await renderExplore(service, 'Mine', { evidence: evidence(), saved: [savedName('Mine', '1')], index: indexOf({}), query: '?view=performance&run=76' });
    await screen.findByRole('region', { name: 'Headline figures' });

    fireEvent.click(viewLink('Backtest'));
    await screen.findByRole('heading', { name: 'Duration' });

    expect(viewLink('Research lineage').getAttribute('href')).toBe('/options-setups/Mine/explore?view=lineage&run=76');
  });

  it('compares the run with another, by the other’s id, in the address', async () => {
    const compare = vi.fn(async () => {
      throw new Error('no comparison in this spec');
    });
    await renderExplore(fakeService(vi, mine()), 'Mine', { evidence: evidence(), detail: { trades: vi.fn(), session: vi.fn(), tradesCsv: vi.fn(), compare } as never, query: '?view=comparison&run=76&comparator_run_id=69' });

    await screen.findByRole('heading', { name: 'Historical comparator' });

    await waitFor(() => expect(compare).toHaveBeenCalledWith(76, 69));
  });

  it('says no run is open when the address names none, and offers History', async () => {
    await renderExplore(fakeService(vi, mine()), 'Mine', { query: '?view=performance' });

    expect(await screen.findByText('No run is open.')).toBeTruthy();
    // Explore's own link to History, and the one the notice offers: both lead to the same place.
    expect(screen.getAllByRole('link', { name: 'History' }).map((link) => link.getAttribute('href'))).toEqual(['/options-setups/Mine/explore?view=history', '/options-setups/Mine/explore?view=history']);
  });

  it('says that a run id which is not one is not, and reads nothing', async () => {
    const reader = evidence();
    await renderExplore(fakeService(vi, mine()), 'Mine', { evidence: reader, query: '?view=performance&run=abc' });

    expect(await screen.findByText('“abc” is not a run id.')).toBeTruthy();
    expect(reader.read).not.toHaveBeenCalled();
  });

  it('says that a view Explore does not have is not one, and offers Backtest', async () => {
    await renderExplore(fakeService(vi, mine()), 'Mine', { query: '?view=chart' });

    expect(await screen.findByText('“chart” is not a view of Explore.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Duration' })).toBeNull();
  });
});

describe('the tab', () => {
  it('is a link in the strip, beside Golden, which has its own address', async () => {
    await renderExplore(fakeService(vi, mine()), 'Mine');

    const tabs = screen.getByRole('navigation', { name: 'Options setup tabs' });

    expect(within(tabs).getByRole('link', { name: 'Explore' }).getAttribute('aria-current')).toBe('page');
    expect(within(tabs).getByRole('link', { name: 'Golden' }).getAttribute('href')).toBe('/options-setups/Mine/golden');
  });

  it('keeps the dates and the answer of Check data when the owner leaves for Overview and comes back', async () => {
    const setup = await readyToStart(savedWorld(), { saved: [savedName('Mine', '1')], index: indexOf({}), bots: { kind: 'known', bots: [] } });
    fireEvent.click(screen.getByRole('button', { name: 'Check data' }));
    await screen.findByRole('region', { name: 'Check data' });

    fireEvent.click(within(screen.getByRole('navigation', { name: 'Options setup tabs' })).getByRole('link', { name: 'Overview' }));
    await screen.findByRole('heading', { name: 'Saved content' });
    await goToExplore();

    expect(screen.getByRole('region', { name: 'Check data' })).toBeTruthy();
    expect((screen.getByLabelText('Start date') as HTMLInputElement).value).toBe('2026-09-14');
    expect(setup.service.checkData).toHaveBeenCalledTimes(1);
  });
});
