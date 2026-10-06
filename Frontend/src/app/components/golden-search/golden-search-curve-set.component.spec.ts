import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchCurveSetComponent } from './golden-search-curve-set.component';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateKey, CurveSet, CurveSetKey, StudyDetail } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { curveSet, studyDetail } from './testing/fixtures';

type Read = (set: CurveSetKey, candidate: CandidateKey | null) => Promise<CurveSet>;

async function renderSet(curves: CurveSetKey, read: Read = async (set) => curveSet(set), candidate: CandidateKey | null = null) {
  const service = { curveSet: vi.fn(async (_id: string, set: CurveSetKey, key: CandidateKey | null) => read(set, key)) };
  const charts = fakeCharts();
  const view = await render(GoldenSearchCurveSetComponent, {
    inputs: { study: studyDetail('awaiting_candidate'), curves, candidate, chart: 'search-curves', caption: 'Every point the search scored.' },
    providers: [{ provide: GoldenSearchService, useValue: service }, ...charts.providers],
  });
  return { view, service, charts };
}

interface DrawnSeries {
  id: string;
  z: number;
  silent?: boolean;
  lineStyle: { color: string; width: number; opacity?: number; type: string };
  endLabel?: { show: boolean };
}

function equitySeries(option: unknown): Map<string, DrawnSeries> {
  const series = (option as { series: DrawnSeries[] }).series.filter((entry) => entry.id.startsWith('equity:'));
  return new Map(series.map((entry) => [entry.id.slice('equity:'.length), entry]));
}

function panel(): HTMLElement {
  return screen.getByRole('region', { name: 'Every scored point' });
}

describe('GoldenSearchCurveSetComponent', () => {
  it('draws a search’s points by role: the winner bold on top, the current settings dashed, the rest faint, quiet and unlabelled', async () => {
    const { service, charts } = await renderSet('search');

    expect((await within(panel()).findByRole('img')).getAttribute('aria-label')).toContain('3 runs drawn at every session close, 2 of them named');
    expect(service.curveSet).toHaveBeenCalledWith('study-0001-aaaa', 'search', null);
    const drawn = equitySeries(charts.charts[0].options.at(-1));
    const [winner, incumbent, other] = [drawn.get('run-winner'), drawn.get('run-incumbent'), drawn.get('run-second')];
    expect([winner?.lineStyle.width, incumbent?.lineStyle.type, other?.lineStyle.opacity]).toEqual([2.5, 'dashed', 0.3]);
    expect((winner?.z ?? 0) > (incumbent?.z ?? 0) && (incumbent?.z ?? 0) > (other?.z ?? 0)).toBe(true);
    expect([winner?.endLabel?.show, other?.endLabel?.show]).toEqual([true, false]);
    // A faint line lets the pointer through, so a crowd of them never dims the winner.
    expect([winner?.silent, other?.silent]).toEqual([undefined, true]);

    // In the table a faint line is told apart by its settings.
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show as table' }));
    const cells = within(panel()).getAllByRole('row').map((row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent?.trim()));
    expect(cells[2]).toEqual(['Scored point', 'Gap $0.10 · RSI 48–72 · EMA 8/21 · hold 4 bars', '+4.1%', '-1.0%']);
  });

  it.each([
    ['recent', '#ffb300', 'solid'],
    // The current settings are dashed wherever they are drawn, also as the candidate the chart is about.
    ['incumbent', '#b2b5be', 'dashed'],
  ] as const)('draws the bold line in the colour of the candidate it is about (%s)', async (candidate, color, type) => {
    const { charts } = await renderSet('candidate', async () => curveSet('candidate'), candidate);

    await within(panel()).findByRole('img');
    const bold = equitySeries(charts.charts[0].options.at(-1)).get('run-candidate');
    expect([bold?.lineStyle.color, bold?.lineStyle.type, bold?.lineStyle.width]).toEqual([color, type, 2.5]);
  });

  it('says what the set could not show: the server’s notes, the points past its cap, and completed runs with no curve', async () => {
    const read = curveSet('search', { left_out: 40, not_recorded: 2, notes: ['The current settings were not run on this window, so they have no line here.'] });
    await renderSet('search', async () => read);

    await within(panel()).findByRole('img');
    expect(panel().textContent).toContain('The current settings were not run on this window, so they have no line here.');
    expect(panel().textContent).toContain('An even spread of the scored points is drawn; 40 more are not. All runs lists every one.');
    expect(panel().textContent).toContain('2 completed runs of this set have no curve recorded.');
  });

  it('a set whose runs have no curve says so and draws nothing', async () => {
    await renderSet('search', async () => curveSet('search', { lines: [], not_recorded: 12 }));

    expect(await screen.findByText('These runs have no curve recorded.')).not.toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('a set with no run yet says it is still to come', async () => {
    await renderSet('search', async () => curveSet('search', { lines: [] }));

    expect(await screen.findByText('No run is recorded for this chart yet.')).not.toBeNull();
  });

  it('reads the selected candidate’s own set, and another candidate’s when the selection changes', async () => {
    const { view, service } = await renderSet('candidate', async (set) => curveSet(set), 'all_period');
    await within(panel()).findByRole('img');

    view.fixture.componentRef.setInput('candidate', 'recent');

    await waitFor(() => expect(service.curveSet.mock.calls.map((call) => call[2])).toEqual(['all_period', 'recent']));
  });

  it('a read that fails says so and can be tried again', async () => {
    let fails = true;
    await renderSet('search', async (set) => {
      if (fails) throw new Error('down');
      return curveSet(set);
    });
    expect((await screen.findByRole('alert')).textContent).toContain('These runs’ curves could not be loaded.');

    fails = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await within(panel()).findByRole('img')).not.toBeNull();
  });

  it('reads again when its own step records more, and not when another part of the study moves', async () => {
    const { view, service } = await renderSet('search');
    await within(panel()).findByRole('img');
    const study = (overrides: Partial<StudyDetail>): StudyDetail => ({ ...studyDetail('awaiting_candidate'), ...overrides });

    // A later stage spends budget and bumps the revision: the search's own points did not change.
    view.fixture.componentRef.setInput('study', study({ revision: 9, consumed_evaluations: 999 }));
    await view.fixture.whenStable();
    expect(service.curveSet).toHaveBeenCalledTimes(1);

    const base = studyDetail('awaiting_candidate');
    const search = base.results.search;
    if (search === null) throw new Error('the fixture has a search');
    view.fixture.componentRef.setInput('study', study({ results: { ...base.results, search: { ...search, evaluations: search.evaluations + 1 } } }));
    await waitFor(() => expect(service.curveSet).toHaveBeenCalledTimes(2));
  });
});
