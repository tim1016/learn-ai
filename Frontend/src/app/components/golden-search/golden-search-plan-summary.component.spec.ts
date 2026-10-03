import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchPlanSummaryComponent } from './golden-search-plan-summary.component';
import { GoldenSearchService } from './golden-search.service';
import type { PlanCharts } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { emaCapability, planCharts, studyDetail } from './testing/fixtures';

async function renderPlan(charts: () => Promise<PlanCharts> = async () => planCharts()) {
  const library = fakeCharts();
  return render(GoldenSearchPlanSummaryComponent, {
    inputs: { study: studyDetail('locked'), capability: emaCapability() },
    providers: [{ provide: GoldenSearchService, useValue: { planCharts: vi.fn(charts) } }, ...library.providers],
  });
}

function tableRows(name: string): string[] {
  const panel = screen.getByRole('region', { name });
  fireEvent.click(within(panel).getByRole('button', { name: 'Show as table' }));
  return within(panel).getAllByRole('row').map((row) => row.textContent ?? '');
}

describe('GoldenSearchPlanSummaryComponent', () => {
  it('charts the frozen plan: its tiles, windows, search space, workload, trade minimums and lake coverage', async () => {
    await renderPlan();

    const tiles = await screen.findByRole('region', { name: 'Plan at a glance' });
    expect(tiles.textContent).toMatch(/Engine runs\s*Up to 900\s*of a 5,000 cap · 420 used so far/);
    expect(tableRows('Window map')[4]).toMatch(/Fold 1 test.*63\s*—/);
    expect(tableRows('Search space')[2]).toMatch(/RSI lower gate\s*held at 50\s*—\s*1/);
    expect(tableRows('Workload')[1]).toMatch(/Search the development period\s*400\s*380/);
    expect(tableRows('Trade minimums')[1]).toMatch(/Development\s*1\.996\s*100\s*2024: 252 of 252 sessions; 2025: 250 of 251 sessions/);
    expect(tableRows('Data coverage')[1]).toMatch(/Jun 2024\s*19\s*18\s*0\s*0\s*1/);
  });

  it('a lake it cannot read says so instead of drawing empty months', async () => {
    await renderPlan(async () => planCharts({ coverage: { status: 'missing', reason: 'The data lake catalog could not be read, so coverage is not shown.' } }));

    const coverage = await screen.findByRole('region', { name: 'Data coverage' });
    expect(coverage.textContent).toContain('could not be read');
    expect(within(coverage).queryByRole('img')).toBeNull();
  });

  it('passes axe with every chart drawn and every table open', async () => {
    const view = await renderPlan();
    await screen.findByRole('region', { name: 'Window map' });
    for (const toggle of screen.getAllByRole('button', { name: 'Show as table' })) fireEvent.click(toggle);

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
