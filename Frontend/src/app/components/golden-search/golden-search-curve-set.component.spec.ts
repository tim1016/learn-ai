import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchCurveSetComponent } from './golden-search-curve-set.component';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateKey, CurveSet, CurveSetKey } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { curveSet, studyDetail } from './testing/fixtures';

async function renderSet(curves: CurveSetKey, read: CurveSet, candidate: CandidateKey | null = null) {
  const service = { curveSet: vi.fn(async () => read) };
  const charts = fakeCharts();
  await render(GoldenSearchCurveSetComponent, {
    inputs: { study: studyDetail('awaiting_candidate'), curves, candidate, chart: 'search-curves', caption: 'Every point the search scored.' },
    providers: [{ provide: GoldenSearchService, useValue: service }, ...charts.providers],
  });
  return { service, charts };
}

interface DrawnSeries {
  id: string;
  z: number;
  lineStyle: { width: number; opacity: number; type: string };
  endLabel?: { show: boolean };
}

describe('GoldenSearchCurveSetComponent', () => {
  it('draws a search’s points by role: the winner bold on top, the current settings dashed, the rest faint and unlabelled', async () => {
    const { service, charts } = await renderSet('search', curveSet('search'));

    const panel = screen.getByRole('region', { name: 'Every scored point' });
    expect((await within(panel).findByRole('img')).getAttribute('aria-label')).toContain('3 runs drawn');
    expect(service.curveSet).toHaveBeenCalledWith('study-0001-aaaa', 'search', null);
    const series = (charts.charts[0].options.at(-1) as { series: DrawnSeries[] }).series.filter((entry) => entry.id.startsWith('equity:'));
    const byKey = new Map(series.map((entry) => [entry.id, entry]));
    const winner = byKey.get('equity:run-winner');
    const incumbent = byKey.get('equity:run-incumbent');
    const other = byKey.get('equity:run-second');
    expect([winner?.lineStyle.width, incumbent?.lineStyle.type, other?.lineStyle.opacity]).toEqual([2.5, 'dashed', 0.3]);
    expect((winner?.z ?? 0) > (incumbent?.z ?? 0) && (incumbent?.z ?? 0) > (other?.z ?? 0)).toBe(true);
    expect([winner?.endLabel?.show, other?.endLabel?.show]).toEqual([true, false]);
  });

  it('asks for the candidate’s own set and says how many runs it left out or has no curve for', async () => {
    const { service } = await renderSet('candidate', curveSet('candidate', { left_out: 40, not_recorded: 2 }), 'recent');

    expect(service.curveSet).toHaveBeenCalledWith('study-0001-aaaa', 'candidate', 'recent');
    const panel = screen.getByRole('region', { name: 'Every scored point' });
    await within(panel).findByRole('img');
    expect(panel.textContent).toContain('40 more scored points are not drawn here; All runs lists every one.');
    expect(panel.textContent).toContain('2 of these runs kept no curve.');
  });

  it('a set recorded before runs kept their curve says so and draws nothing', async () => {
    await renderSet('search', curveSet('search', { lines: [], not_recorded: 12 }));

    expect(await screen.findByText('These runs were recorded before runs kept their curve.')).not.toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
  });
});
