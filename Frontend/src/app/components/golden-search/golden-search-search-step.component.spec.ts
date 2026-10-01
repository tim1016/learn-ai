import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { GoldenSearchSearchStepComponent } from './golden-search-search-step.component';
import type { StudyDetail } from './golden-search.types';
import { emaCapability, metrics, searchView, studyDetail } from './testing/fixtures';

async function renderStep(study: StudyDetail) {
  return render(GoldenSearchSearchStepComponent, { inputs: { study, capability: emaCapability() } });
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

    expect(within(allPeriod).getByText(/stopped: no improvement/i).parentElement?.textContent).toContain('A full pass moved no knob');
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
    expect(within(recent).getByText(/stopped: pass limit/i)).not.toBeNull();
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

    expect(screen.getByRole('group', { name: /parameter map: fast ema length × slow ema length/i })).not.toBeNull();
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

    expect(screen.queryByRole('group', { name: /parameter map/i })).toBeNull();
    expect(screen.queryByRole('complementary')).toBeNull();
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
