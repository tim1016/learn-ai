import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchChanceFinalPanelComponent } from './golden-search-chance-final-panel.component';
import { chanceCarryOver, chanceFitToFit, finalChance } from './testing/fixtures';
import { fakeCharts } from './testing/fake-charts';
import type { CandidateRef, ExamChance, FinalChance } from './golden-search.types';

const CANDIDATE: CandidateRef = { key: 'all_period', label: 'All-period fit' };

async function renderPanel(chance: FinalChance, examChance: ExamChance = { fit_to_fit: chanceFitToFit() }, candidate: CandidateRef = CANDIDATE) {
  return render(GoldenSearchChanceFinalPanelComponent, {
    inputs: { candidate, chance, examChance },
    providers: fakeCharts().providers,
  });
}

describe('GoldenSearchChanceFinalPanelComponent', () => {
  it('shows the stored comparison sentence, both bands, the profitability line and the carry-over with its curve', async () => {
    await renderPanel(finalChance());
    expect(screen.getByText(/at least as good as the current settings’ in 96\.2% of paths/i)).toBeTruthy();
    expect(screen.getByRole('img', { name: /final uncertainty/i })).toBeTruthy();
    expect(screen.getByRole('img', { name: /result among its rehearsals/i })).toBeTruthy();
    expect(screen.getByText(/positive in 97\.4% of 10,000 resampled development paths/i)).toBeTruthy();
  });

  it('keeps the frozen-verdict separation and the selection caveats visible', async () => {
    await renderPanel(finalChance());
    expect(screen.getByText(/frozen "meets the rules" verdict above is\s+separate from this model support/i)).toBeTruthy();
    expect(screen.getAllByText(/chosen after the final test/i).length).toBeGreaterThan(0);
  });

  it('shows the fit-to-fit sentence from the shared draw when two fits ran', async () => {
    await renderPanel(finalChance());
    expect(screen.getByText(/All-period fit was at least as good as Recent fit in 68\.4% of paths/i)).toBeTruthy();
    expect(screen.getByText(/not adjusted for that choice/i)).toBeTruthy();
  });

  it('reads each fit its own way: a weak carry-over shows its own words, never another fit’s', async () => {
    await renderPanel(
      finalChance({
        carry_over: chanceCarryOver({
          rank: 0.0412,
          below: 412,
          label: 'below_most',
          sentence: 'The recorded final total return sits below most development rehearsals: strictly above 412 of the 10,000 defined rehearsal paths (4.1%), 0 undefined.',
        }),
      }),
    );
    expect(screen.getByText(/below most development rehearsals/i)).toBeTruthy();
  });

  it('says so plainly when nothing was measured', async () => {
    await renderPanel(
      {
        comparison: { status: 'missing', code: 'not_measured', reason: 'Not measured for this study.' },
        profitability: { status: 'missing', code: 'not_measured', reason: 'Not measured for this study.' },
        carry_over: { status: 'missing', code: 'carry_over_unavailable', reason: 'No matching stored rehearsal could carry over.' },
      },
      { fit_to_fit: { status: 'missing', code: 'single_fit', reason: 'Only one distinct fit sat this final test.' } },
    );
    expect(screen.getByText(/No final uncertainty was measured for this fit/i)).toBeTruthy();
  });

  it('passes an accessibility audit in its measured state', async () => {
    const view = await renderPanel(finalChance());
    const results = await axe.run(view.fixture.nativeElement, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
