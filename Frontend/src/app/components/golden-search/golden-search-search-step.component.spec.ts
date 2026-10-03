import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchSearchStepComponent } from './golden-search-search-step.component';
import { GoldenSearchService } from './golden-search.service';
import type { SearchCharts, StudyDetail } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { emaCapability, metrics, procedureCharts, searchCharts, searchView, studyDetail } from './testing/fixtures';

async function renderStep(study: StudyDetail, charts: () => Promise<SearchCharts> = async () => searchCharts()) {
  const library = fakeCharts();
  const service = { searchCharts: vi.fn(charts) };
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
  it('shows where the search went: each value tried with its eligibility, the constraint-skipped values, the value kept and whether it moved', async () => {
    await renderStep(studyDetail('awaiting_validation'));
    const allPeriod = screen.getByRole('region', { name: 'All-period search' });

    const fast = pathRow(allPeriod, 'Fast EMA length');
    expect(fast.textContent).toContain('3 – 12');
    expect(fast.textContent).toMatch(/3\s*Too Few Trades/);
    expect(fast.textContent).toMatch(/8\s*eligible/);
    expect(fast.textContent).toContain('The fast EMA must be shorter than the slow EMA.');
    expect(fast.textContent).toMatch(/8\s*moved/);
    expect(pathRow(allPeriod, 'Slow EMA length').textContent).toMatch(/10\s*kept current/);
  });

  it('summarizes each searched knob (start, value kept, why it stopped) and counts the evaluated, cached and invalid points', async () => {
    await renderStep(studyDetail('awaiting_validation'));
    const allPeriod = screen.getByRole('region', { name: 'All-period search' });

    expect(allPeriod.textContent).toContain('Zoom Search · 486 evaluated · 134 cached · 18 invalid');
    expect(allPeriod.textContent).toContain('2 complete passes. “No improvement” describes the moves tested, not the best configuration everywhere.');
    const summary = within(allPeriod).getByRole('table', { name: /each searched knob/i });
    const fast = within(summary).getByRole('rowheader', { name: /fast ema length/i }).closest('tr');
    expect(fast?.textContent).toMatch(/5\s*8\s*moved\s*No better tested move/);
    const slow = within(summary).getByRole('rowheader', { name: /slow ema length/i }).closest('tr');
    expect(slow?.textContent).toMatch(/10\s*10\s*kept current\s*Minimum step reached/);
  });

  it('says why the search stopped and warns about a knob that ended at the edge of its searched range', async () => {
    await renderStep(studyDetail('awaiting_validation'));
    const allPeriod = screen.getByRole('region', { name: 'All-period search' });

    expect(within(allPeriod).getByText('A full pass moved no knob, so the search kept its last settings.')).not.toBeNull();
    expect(within(allPeriod).getByRole('note').textContent).toContain('RSI upper gate ended at the edge of its searched range');
  });

  it('lists the retained settings by trader name, with the development metrics they earned', async () => {
    await renderStep(studyDetail('awaiting_validation'));
    const allPeriod = screen.getByRole('region', { name: 'All-period search' });

    const settings = within(allPeriod).getByRole('heading', { name: 'Retained settings' }).parentElement;
    expect(settings?.textContent).toMatch(/Fast EMA length\s*8/);
    expect(settings?.textContent).toMatch(/Slow EMA length\s*21/);
    expect(settings?.textContent).not.toContain('symbol');
    expect(settings?.textContent).toMatch(/Sharpe\s*1\.18/);
  });

  it('shows the recent-window procedure as its own in-sample result', async () => {
    await renderStep(studyDetail('awaiting_validation'));

    const recent = screen.getByRole('region', { name: 'Recent window' });
    expect(recent.textContent).toContain('last 6 months of development');
    expect(within(recent).getByText('The pass limit was reached while knobs still moved.')).not.toBeNull();
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

  it('shows the all-period pair landscape beside why a pair grid checks a one-knob path', async () => {
    await renderStep(studyDetail('awaiting_validation'));

    const landscape = await screen.findByRole('region', { name: 'Pair landscape' });
    expect(within(landscape).getByRole('img').getAttribute('aria-label')).toContain('Fast EMA length × Slow EMA length landscape');
    expect(tableRows(landscape).some((row) => row.includes('Invalid'))).toBe(true);
    const aside = screen.getByRole('complementary', { name: 'Use Grid to challenge Zoom' });
    expect(aside.textContent).toMatch(/Pair audit · Fast EMA length × Slow EMA length\s*21 valid \/ 4 invalid/);
    expect(aside.textContent).toMatch(/Other settings\s*Held fixed/);
    expect(aside.textContent).toMatch(/Global optimum\s*Not established/);
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

  it('charts each procedure: its path with the best so far, knob moves, profiles and every scored point against the rules', async () => {
    await renderStep(studyDetail('awaiting_validation'));

    const path = await screen.findByRole('region', { name: 'Search path' });
    expect(within(path).getByRole('img').getAttribute('aria-label')).toContain('All-period: 3 points tried in order, each by its Sharpe; the best eligible Sharpe so far ends at 1.18.');
    expect(tableRows(path)[2]).toMatch(/Pass 1, Fast EMA length round 1\s*3\s*1\.60\s*too few trades\s*0\.90/);
    expect(tableRows(screen.getByRole('region', { name: 'Knob moves' }))[1]).toMatch(/Fast EMA length\s*3 to 12\s*5\s*8\s*no/);
    expect(tableRows(screen.getByRole('region', { name: 'One-knob profiles' })).at(-1)).toMatch(/Fast EMA length\s*8\s*1\.18\s*meets the rules\s*yes/);
    expect(tableRows(screen.getByRole('region', { name: 'Eligibility map' })).at(-1)).toContain('(winner)');
    // The recent fit's panels are named apart from the all-period search's.
    expect(screen.getByRole('region', { name: 'Search path · Recent window' })).not.toBeNull();
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
