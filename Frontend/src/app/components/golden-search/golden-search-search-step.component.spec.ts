import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { GoldenSearchSearchStepComponent } from './golden-search-search-step.component';
import type { StudyDetail } from './golden-search.types';
import { emaCapability, metrics, procedureView, studyDetail } from './testing/fixtures';

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
    const grid = procedureView({ rounds: [], edge_hits: [], winner_metrics: metrics({ status: 'failed', error: 'engine refused the window', sharpe_ratio: null }) });
    await renderStep(studyDetail('awaiting_validation', { method: 'grid', results: { ...studyDetail('awaiting_validation').results, search: grid, recent: null }, protocol: { ...studyDetail('locked').protocol, recent_window: false } }));

    expect(screen.getByText(/no path to show/i)).not.toBeNull();
    expect(screen.queryByRole('table', { name: /round by round/i })).toBeNull();
    expect(screen.getByText(/run failed — engine refused the window/i)).not.toBeNull();
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
