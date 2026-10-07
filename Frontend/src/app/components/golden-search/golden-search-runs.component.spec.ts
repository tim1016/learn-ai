import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../shared/date/et-midnight';
import { GoldenSearchRunsComponent } from './golden-search-runs.component';
import { GoldenSearchService } from './golden-search.service';
import type { RunCurves, RunsPage, RunsQuery, StudyDetail } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { runCurve, runCurves, runsPage, studyDetail, studyRun } from './testing/fixtures';

async function renderRuns(
  study: StudyDetail = studyDetail('awaiting_candidate'),
  page: (query: RunsQuery) => RunsPage = () => runsPage(),
  curves: (keys: readonly string[]) => Promise<RunCurves> = async (keys) => runCurves(keys),
) {
  const service = {
    runs: vi.fn(async (_id: string, query: RunsQuery) => page(query)),
    runCurves: vi.fn(async (_id: string, keys: readonly string[]) => curves(keys)),
  };
  const charts = fakeCharts();
  const view = await render(GoldenSearchRunsComponent, {
    inputs: { study },
    providers: [{ provide: GoldenSearchService, useValue: service }, ...charts.providers],
  });
  await screen.findByRole('table');
  return { view, service, charts };
}

function rowOf(label: string): HTMLElement {
  const row = screen.getByRole('rowheader', { name: new RegExp(`^${label}`) }).closest('tr');
  if (row === null) throw new Error(`no row for ${label}`);
  return row;
}

describe('GoldenSearchRunsComponent', () => {
  it('lists every run under what asked for it, with its settings, window and numbers', async () => {
    const { view } = await renderRuns();

    const filters = screen.getByRole('group', { name: 'What asked for the runs' });
    expect(within(filters).getAllByRole('button').map((chip) => chip.textContent?.replace(/\s+/g, ' ').trim())).toEqual(['All 5', 'All-period search 4', 'Fold test 1']);
    const winner = rowOf('All-period search · All-period fit');
    expect(winner.textContent).toMatch(/Gap \$0\.15 · RSI 48–72 · EMA 8\/21 · hold 4 bars\s*2024-01-01\s*–\s*2025-12-31\s*\+8\.7%\s*1\.18\s*6\.4%\s*146/);
    expect(rowOf('Fold 1 test · Fold winner').textContent).toMatch(/2025-07-01\s*–\s*2025-09-30/);
    expect(screen.getByText('1–5 of 5')).not.toBeNull();
    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });

  it('draws the runs you tick together, reads each curve once, and clears', async () => {
    // The fold's curve comes back from its own read, against that read's sessions and not from their start.
    const foldSessions = ['2025-06-30', '2025-07-01', '2025-08-01', '2025-09-30'].map((day) => etMidnightMs(day));
    const { service, charts } = await renderRuns(undefined, undefined, async (keys) =>
      keys[0] === 'run-fold' ? { sessions_ms: foldSessions, curves: [{ ...runCurve('run-fold'), first_session: 1 }], not_recorded: [] } : runCurves(keys),
    );
    expect(screen.getByText('Tick runs in the list below to draw them here.')).not.toBeNull();

    fireEvent.click(screen.getByRole('checkbox', { name: /^Draw All-period search · All-period fit, Gap \$0\.15/ }));
    const chart = await within(screen.getByRole('region', { name: 'Run equity curves' })).findByRole('img');
    expect(chart.getAttribute('aria-label')).toContain('1 run drawn');
    fireEvent.click(screen.getByRole('checkbox', { name: /^Draw Fold 1 test · Fold winner/ }));
    await waitFor(() => expect(within(screen.getByRole('region', { name: 'Run equity curves' })).getByRole('img').getAttribute('aria-label')).toContain('2 runs drawn'));

    // The second tick asks only for the curve it does not hold yet.
    expect(service.runCurves.mock.calls.map((call) => call[1])).toEqual([['run-winner'], ['run-fold']]);
    expect(screen.getByText('2 of at most 100 drawn')).not.toBeNull();
    // A hover names each run by what asked for it and by its settings: two runs of one step never read the same.
    const tooltip = charts.charts.at(-1)?.options.at(-1)?.tooltip;
    const formatter = !Array.isArray(tooltip) && typeof tooltip?.formatter === 'function' ? tooltip.formatter : null;
    // The library hands a hover far more than the index this one reads.
    const hoverAt = (dataIndex: number): unknown => (formatter === null ? null : Reflect.apply(formatter, undefined, [[{ dataIndex }]]));
    // The two runs share the date axis and no session: the search's first session, then the fold's.
    expect(hoverAt(0)).toContain('All-period search · All-period fit · Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars');
    expect(hoverAt(2)).toContain('Fold 1 test · Fold winner · Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars');
    // The table names each drawn run by what asked for it and by its settings, with the server's deepest fall.
    const panel = screen.getByRole('region', { name: 'Run equity curves' });
    fireEvent.click(within(panel).getByRole('button', { name: 'Show as table' }));
    const cells = within(panel).getAllByRole('row').map((row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent?.trim()));
    expect(cells).toEqual([
      ['Run', 'Settings', 'First session', 'Last session', 'Ended at', 'Deepest fall'],
      ['All-period search · All-period fit', 'Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars', '2024-01-02', '2025-12-31', '+8.7%', '-2.2%'],
      ['Fold 1 test · Fold winner', 'Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars', '2025-07-01', '2025-09-30', '+8.7%', '-2.2%'],
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(screen.getByText('Tick runs in the list below to draw them here.')).not.toBeNull();
  });

  it('a run recorded before runs kept their curve says so and cannot be ticked; a failed run gives its reason', async () => {
    const { service } = await renderRuns();

    const old = within(rowOf('All-period search\\s*Curve not recorded')).getByRole('checkbox') as HTMLInputElement;
    expect(old.disabled).toBe(true);
    // The failed run never had a curve to keep, so only the completed run without one counts.
    expect(screen.getByText(/Completed runs with no curve recorded: 1\./)).not.toBeNull();
    expect(screen.getByText('Run failed — no bars')).not.toBeNull();

    // Drawing the page takes only the runs that have a curve.
    fireEvent.click(screen.getByRole('button', { name: 'Draw every run on this page' }));
    await waitFor(() => expect(service.runCurves).toHaveBeenCalledWith('study-0001-aaaa', ['run-winner', 'run-second', 'run-fold']));
  });

  it('filters by what asked for the runs and sorts by a measure, better end first, keeping the ticks', async () => {
    const { service } = await renderRuns(studyDetail('awaiting_candidate'), (query) =>
      query.group === 'fold_test' ? runsPage({ total: 1, rows: [studyRun('run-fold', { label: 'Fold 1 test · Fold winner' })] }) : runsPage(),
    );
    fireEvent.click(screen.getByRole('checkbox', { name: /^Draw All-period search · All-period fit/ }));
    expect(screen.queryByRole('group', { name: 'Which fold' })).toBeNull();

    fireEvent.click(within(screen.getByRole('group', { name: 'What asked for the runs' })).getByRole('button', { name: /Fold test/ }));
    await waitFor(() => expect(screen.getByText('1–1 of 1')).not.toBeNull());
    // A fold group can be listed one fold at a time.
    fireEvent.click(within(screen.getByRole('group', { name: 'Which fold' })).getByRole('button', { name: 'Fold 2' }));
    fireEvent.click(screen.getByRole('button', { name: /Worst fall/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Net return/ }));

    await waitFor(() => expect(service.runs.mock.calls.map((call) => [call[1].group, call[1].fold_index, call[1].sort, call[1].descending])).toEqual([
      [null, null, 'recorded', false],
      ['fold_test', null, 'recorded', false],
      ['fold_test', 1, 'recorded', false],
      // The smallest fall first, the largest return first.
      ['fold_test', 1, 'max_drawdown_pct', false],
      ['fold_test', 1, 'total_return_pct', true],
    ]));
    expect(screen.getByText('1 of at most 100 drawn')).not.toBeNull();
  });

  it('turns the page, and keeps the page on screen until the next one arrives', async () => {
    const { service } = await renderRuns(studyDetail('awaiting_candidate'), (query) => runsPage({ total: 250, page: query.page }));
    expect(screen.getByText('1–100 of 250')).not.toBeNull();
    expect((screen.getByRole('button', { name: 'Previous' }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('table')).not.toBeNull();

    await waitFor(() => expect(screen.getByText('101–200 of 250')).not.toBeNull());
    expect(service.runs.mock.calls.at(-1)?.[1].page).toBe(2);
  });

  it('a list that cannot be read says so and can be tried again; so can the curves', async () => {
    let listFails = true;
    let curvesFail = true;
    const service = {
      runs: vi.fn(async () => {
        if (listFails) throw new Error('down');
        return runsPage();
      }),
      runCurves: vi.fn(async (_id: string, keys: readonly string[]): Promise<RunCurves> => {
        if (curvesFail) throw new Error('down');
        return runCurves(keys);
      }),
    };
    await render(GoldenSearchRunsComponent, {
      inputs: { study: studyDetail('awaiting_candidate') },
      providers: [{ provide: GoldenSearchService, useValue: service }, ...fakeCharts().providers],
    });
    expect((await screen.findByRole('alert')).textContent).toContain('The study’s runs could not be loaded.');

    listFails = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('checkbox', { name: /^Draw All-period search · All-period fit/ }));

    expect((await screen.findByRole('alert')).textContent).toContain('The ticked runs’ curves could not be loaded.');
    expect(screen.getByRole('table')).not.toBeNull();

    curvesFail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    const chart = await within(screen.getByRole('region', { name: 'Run equity curves' })).findByRole('img');
    expect(chart.getAttribute('aria-label')).toContain('1 run drawn');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('a filter whose read fails shows no rows of the filter before it, and a failed re-read of the same list keeps them', async () => {
    let fails = false;
    const { view } = await renderRuns(studyDetail('awaiting_candidate'), () => {
      if (fails) throw new Error('down');
      return runsPage();
    });

    // The study moves on and the same list cannot be read again: its rows still answer the controls.
    fails = true;
    view.fixture.componentRef.setInput('study', { ...studyDetail('awaiting_candidate'), revision: 99 });
    expect((await screen.findByRole('alert')).textContent).toContain('The study’s runs could not be loaded.');
    expect(screen.getByRole('table')).not.toBeNull();

    // Another filter cannot be read: the rows on screen are the earlier filter's, so they go.
    fireEvent.click(within(screen.getByRole('group', { name: 'What asked for the runs' })).getByRole('button', { name: /Fold test/ }));
    await waitFor(() => expect(screen.queryByRole('table')).toBeNull());
    expect(screen.getByRole('alert')).not.toBeNull();
    expect((screen.getByRole('button', { name: 'Draw every run on this page' }) as HTMLButtonElement).disabled).toBe(true);

    fails = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('table')).not.toBeNull();
  });

  it('says so when the study has recorded no run yet', async () => {
    const service = { runs: vi.fn(async () => runsPage({ total: 0, groups: [], rows: [] })), runCurves: vi.fn() };
    await render(GoldenSearchRunsComponent, {
      inputs: { study: studyDetail('locked') },
      providers: [{ provide: GoldenSearchService, useValue: service }, ...fakeCharts().providers],
    });

    expect(await screen.findByText(/No run is recorded for this study yet\./)).not.toBeNull();
  });
});
