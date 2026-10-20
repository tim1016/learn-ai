import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchSearchStepComponent } from './golden-search-search-step.component';
import { GoldenSearchService } from './golden-search.service';
import type { CurveSetKey, SearchCharts, StudyDetail } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { curveSet, emaCapability, metrics, procedureCharts, searchCharts, searchView, studyDetail } from './testing/fixtures';

async function renderStep(study: StudyDetail, charts: () => Promise<SearchCharts> = async () => searchCharts()) {
  const library = fakeCharts();
  const service = { searchCharts: vi.fn(charts), curveSet: vi.fn(async (_id: string, set: CurveSetKey) => curveSet(set)) };
  return render(GoldenSearchSearchStepComponent, {
    inputs: { study, capability: emaCapability() },
    providers: [{ provide: GoldenSearchService, useValue: service }, ...library.providers],
  });
}

function tableRows(panel: HTMLElement): string[] {
  fireEvent.click(within(panel).getByRole('button', { name: 'Show as table' }));
  return within(panel).getAllByRole('row').map((row) => row.textContent ?? '');
}

function pathRow(section: HTMLElement, knob: string): HTMLElement {
  const table = within(section).getByRole('table', { name: /round by round/i });
  const row = within(table).getByRole('rowheader', { name: knob }).closest('tr');
  if (row === null) throw new Error(`no path row for ${knob}`);
  return row;
}

describe('GoldenSearchSearchStepComponent', () => {
  it('shows a finished search: where it went, its summary and stop reason, the retained settings with their metrics, and the recent window as its own in-sample result', async () => {
    await renderStep(studyDetail('awaiting_validation'));
    const allPeriod = screen.getByRole('region', { name: 'All-period search' });

    const fastPath = pathRow(allPeriod, 'Fast EMA length');
    expect.soft(fastPath.textContent, 'shows the range of each value tried').toContain('3 – 12');
    expect.soft(fastPath.textContent, 'shows a value tried with its ineligibility').toMatch(/3\s*Too Few Trades/);
    expect.soft(fastPath.textContent, 'shows a value tried with its eligibility').toMatch(/8\s*eligible/);
    expect.soft(fastPath.textContent, 'shows the constraint-skipped values').toContain('The fast EMA must be shorter than the slow EMA.');
    expect.soft(fastPath.textContent, 'shows the value kept and that it moved').toMatch(/8\s*moved/);
    expect.soft(pathRow(allPeriod, 'Slow EMA length').textContent, 'shows the value kept and that it did not move').toMatch(/10\s*kept current/);

    expect.soft(allPeriod.textContent, 'counts the evaluated, cached and invalid points').toContain('Zoom Search · 486 evaluated · 134 cached · 18 invalid');
    expect.soft(allPeriod.textContent, 'counts the passes and says what no improvement describes').toContain('2 complete passes. “No improvement” describes the moves tested, not the best configuration everywhere.');
    const summary = within(allPeriod).getByRole('table', { name: /each searched knob/i });
    const fastSummary = within(summary).getByRole('rowheader', { name: /fast ema length/i }).closest('tr');
    expect.soft(fastSummary?.textContent, 'summarizes the fast knob: start, value kept, why it stopped').toMatch(/5\s*8\s*moved\s*No better tested move/);
    const slowSummary = within(summary).getByRole('rowheader', { name: /slow ema length/i }).closest('tr');
    expect.soft(slowSummary?.textContent, 'summarizes the slow knob: start, value kept, why it stopped').toMatch(/10\s*10\s*kept current\s*Minimum step reached/);

    expect.soft(within(allPeriod).getByText('A full pass moved no knob, so the search kept its last settings.'), 'says why the search stopped').not.toBeNull();
    expect.soft(within(allPeriod).getByRole('note').textContent, 'warns about a knob that ended at the edge of its searched range').toContain('RSI upper gate ended at the edge of its searched range');

    const settings = within(allPeriod).getByRole('heading', { name: 'Retained settings' }).parentElement;
    expect.soft(settings?.textContent, 'lists the fast retained setting by trader name').toMatch(/Fast EMA length\s*8/);
    expect.soft(settings?.textContent, 'lists the slow retained setting by trader name').toMatch(/Slow EMA length\s*21/);
    expect.soft(settings?.textContent, 'leaves the symbol out of the retained settings').not.toContain('symbol');
    expect.soft(settings?.textContent, 'shows the development metrics the retained settings earned').toMatch(/Sharpe\s*1\.18/);

    const recent = screen.getByRole('region', { name: 'Recent window' });
    expect.soft(recent.textContent, 'shows the recent-window procedure as its own in-sample result').toContain('last 6 months of development');
    expect.soft(within(recent).getByText('The pass limit was reached while knobs still moved.'), 'says why the recent-window search stopped').not.toBeNull();
  });

  it('says a Grid study has no path rather than inventing one, and shows a failed winner run as failed', async () => {
    const grid = searchView({ pair_maps: [], rounds: [], edge_hits: [], knob_summary: [], winner_metrics: metrics({ status: 'failed', error: 'engine refused the window', sharpe_ratio: null }) });
    await renderStep(studyDetail('awaiting_validation', { method: 'grid', results: { ...studyDetail('awaiting_validation').results, search: grid, recent: null }, protocol: { ...studyDetail('locked').protocol, recent_window: false } }));

    expect(screen.getByText(/no path to show/i)).not.toBeNull();
    expect(screen.queryByText(/complete pass/)).toBeNull();
    expect(screen.getByText(/grid search · 486 evaluated/i)).not.toBeNull();
    expect(screen.queryByRole('table', { name: /round by round/i })).toBeNull();
    expect(screen.getByText(/run failed — engine refused the window/i)).not.toBeNull();
  });

  it('charts each procedure: the all-period pair landscape beside why a pair grid checks a one-knob path, and the path with the best so far, knob moves, profiles and every scored point against the rules', async () => {
    await renderStep(studyDetail('awaiting_validation'));

    const landscape = await screen.findByRole('region', { name: 'Pair landscape' });
    expect.soft(within(landscape).getByRole('img').getAttribute('aria-label'), 'names the pair landscape chart').toContain('Fast EMA length × Slow EMA length landscape');
    expect.soft(tableRows(landscape).some((row) => row.includes('Invalid')), 'lists the invalid cells of the pair landscape in its table').toBe(true);
    const aside = screen.getByRole('complementary', { name: 'Use Grid to challenge Zoom' });
    expect.soft(aside.textContent, 'counts the valid and invalid cells of the pair audit').toMatch(/Pair audit · Fast EMA length × Slow EMA length\s*21 valid \/ 4 invalid/);
    expect.soft(aside.textContent, 'says the other settings are held fixed').toMatch(/Other settings\s*Held fixed/);
    expect.soft(aside.textContent, 'says the global optimum is not established').toMatch(/Global optimum\s*Not established/);

    const path = await screen.findByRole('region', { name: 'Search path' });
    expect.soft(within(path).getByRole('img').getAttribute('aria-label'), 'names the search path chart with the best so far').toContain('All-period: 3 points tried in order, each by its Sharpe; the best eligible Sharpe so far ends at 1.18.');
    // The table holds what the hover shows: the settings and every number.
    expect.soft(tableRows(path)[2], 'tables the search path with its settings and every number').toMatch(/Pass 1, Fast EMA length round 1\s*3.*Fast EMA length.*1\.60.*too few trades\s*0\.90/);
    expect.soft(tableRows(screen.getByRole('region', { name: 'Knob moves' }))[1], 'tables the knob moves').toMatch(/Fast EMA length\s*3 to 12\s*5\s*8\s*no/);
    expect.soft(tableRows(screen.getByRole('region', { name: 'One-knob profiles' })).at(-1), 'tables the one-knob profiles').toMatch(/Fast EMA length\s*8\s*1\.18.*meets the rules\s*yes/);
    expect.soft(tableRows(screen.getByRole('region', { name: 'Eligibility map' })).at(-1), 'tables every scored point against the rules, marking the winner').toContain('(winner)');
    // The recent fit's panels are named apart from the all-period search's.
    expect.soft(screen.getByRole('region', { name: 'Search path · Recent window' }), "names the recent fit's panels apart from the all-period search's").not.toBeNull();
  });

  it('a pair audit cut short by the budget says its missing cells read as untested; a complete one says nothing', async () => {
    const study = studyDetail('awaiting_validation');
    const view = await renderStep({ ...study, results: { ...study.results, search: searchView({ pair_maps_incomplete: true }) } });
    const note = /pair audit stopped at its evaluation budget, so some cells are missing and are shown as untested/i;

    expect(screen.getByText(note)).not.toBeNull();
    view.fixture.componentRef.setInput('study', study);
    await view.fixture.whenStable();
    expect(screen.queryByText(note)).toBeNull();
  });

  it('a procedure without pair audits shows no landscape', async () => {
    const study = studyDetail('awaiting_validation');
    await renderStep({ ...study, results: { ...study.results, search: searchView({ pair_maps: [] }) } });

    await screen.findByRole('region', { name: 'Search path' });
    expect(screen.queryByRole('region', { name: 'Pair landscape' })).toBeNull();
    expect(screen.queryByRole('complementary')).toBeNull();
  });

  it('a Grid study draws no search path, and a Zoom path that cannot be rebuilt says why', async () => {
    const grid = procedureCharts('search', { method: 'grid', convergence: { status: 'missing', reason: 'Grid scores every combination at once.' } });
    const unrebuilt = procedureCharts('recent', { convergence: { status: 'missing', reason: 'The recorded path does not rebuild the recorded winner.' }, profiles: [] });
    await renderStep(studyDetail('awaiting_validation'), async () => searchCharts([grid, unrebuilt]));

    await screen.findByRole('region', { name: 'Knob moves' });
    expect(screen.queryByRole('region', { name: 'Search path' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Search path · Recent window' }).textContent).toContain('does not rebuild the recorded winner');
  });

  it('passes axe with every chart drawn and every table open', async () => {
    const view = await renderStep(studyDetail('awaiting_validation'));
    await screen.findByRole('region', { name: 'Search path' });
    for (const toggle of screen.getAllByRole('button', { name: 'Show as table' })) fireEvent.click(toggle);

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });

  it('before the search has run, says so and still accounts for the engine runs', async () => {
    await renderStep(studyDetail('locked'));

    expect(screen.getByRole('status').textContent).toContain('has not started');
    const counts = screen.getByRole('region', { name: /engine runs for this study/i });
    expect(counts.textContent).toMatch(/420 of 5,000/);
    expect(counts.textContent).toMatch(/Answered from cache\s*37/);
    expect(counts.textContent).toMatch(/Skipped as invalid\s*6/);
  });
});
