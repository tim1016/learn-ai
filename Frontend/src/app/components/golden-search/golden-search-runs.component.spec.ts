import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchRunsComponent } from './golden-search-runs.component';
import { GoldenSearchService } from './golden-search.service';
import type { RunsPage, RunsQuery, StudyDetail } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { runCurves, runsPage, studyDetail, studyRun } from './testing/fixtures';

async function renderRuns(study: StudyDetail = studyDetail('awaiting_candidate'), page: (query: RunsQuery) => RunsPage = () => runsPage()) {
  const service = {
    runs: vi.fn(async (_id: string, query: RunsQuery) => page(query)),
    runCurves: vi.fn(async (_id: string, keys: readonly string[]) => runCurves(keys)),
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
    const { service } = await renderRuns();
    expect(screen.getByText('Tick runs in the list below to draw them here.')).not.toBeNull();

    fireEvent.click(screen.getByRole('checkbox', { name: 'Draw All-period search · All-period fit' }));
    const chart = await within(screen.getByRole('region', { name: 'Run equity curves' })).findByRole('img');
    expect(chart.getAttribute('aria-label')).toContain('1 run drawn');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Draw Fold 1 test · Fold winner' }));
    await waitFor(() => expect(within(screen.getByRole('region', { name: 'Run equity curves' })).getByRole('img').getAttribute('aria-label')).toContain('2 runs drawn'));

    // The second tick asks only for the curve it does not hold yet.
    expect(service.runCurves.mock.calls.map((call) => call[1])).toEqual([['run-winner'], ['run-fold']]);
    expect(screen.getByText('2 of at most 100 drawn')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(screen.getByText('Tick runs in the list below to draw them here.')).not.toBeNull();
  });

  it('a run recorded before runs kept their curve says so and cannot be ticked; a failed run gives its reason', async () => {
    const { service } = await renderRuns();

    const old = within(rowOf('All-period search\\s*Curve not recorded')).getByRole('checkbox') as HTMLInputElement;
    expect(old.disabled).toBe(true);
    // The failed run never had a curve to keep, so only the older run counts.
    expect(screen.getByText(/Recorded before runs kept their curve: 1\./)).not.toBeNull();
    expect(screen.getByText('Run failed — no bars')).not.toBeNull();

    // Drawing the page takes only the runs that have a curve.
    fireEvent.click(screen.getByRole('button', { name: 'Draw every run on this page' }));
    await waitFor(() => expect(service.runCurves).toHaveBeenCalledWith('study-0001-aaaa', ['run-winner', 'run-second', 'run-fold']));
  });

  it('filters by what asked for the runs and sorts by a measure, better end first, keeping the ticks', async () => {
    const { service } = await renderRuns(studyDetail('awaiting_candidate'), (query) =>
      query.group === 'fold_test' ? runsPage({ total: 1, rows: [studyRun('run-fold', { label: 'Fold 1 test · Fold winner' })] }) : runsPage(),
    );
    fireEvent.click(screen.getByRole('checkbox', { name: 'Draw All-period search · All-period fit' }));

    fireEvent.click(within(screen.getByRole('group', { name: 'What asked for the runs' })).getByRole('button', { name: /Fold test/ }));
    await waitFor(() => expect(screen.getByText('1–1 of 1')).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /Worst fall/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Net return/ }));

    await waitFor(() => expect(service.runs.mock.calls.map((call) => [call[1].group, call[1].sort, call[1].descending])).toEqual([
      [null, 'recorded', false],
      ['fold_test', 'recorded', false],
      // The smallest fall first, the largest return first.
      ['fold_test', 'max_drawdown_pct', false],
      ['fold_test', 'total_return_pct', true],
    ]));
    expect(screen.getByText('1 of at most 100 drawn')).not.toBeNull();
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
