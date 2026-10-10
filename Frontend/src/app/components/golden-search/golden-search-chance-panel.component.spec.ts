import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchChancePanelComponent } from './golden-search-chance-panel.component';
import { chanceComparison, chanceReadings } from './testing/fixtures';
import { fakeCharts } from './testing/fake-charts';
import type { CandidateRef, ChanceReadings } from './golden-search.types';

const CANDIDATE: CandidateRef = { key: 'all_period', label: 'All-period fit' };

async function renderPanel(chance: ChanceReadings, candidate: CandidateRef = CANDIDATE) {
  return render(GoldenSearchChancePanelComponent, {
    inputs: { candidate, chance },
    providers: fakeCharts().providers,
  });
}

describe('GoldenSearchChancePanelComponent', () => {
  it('shows the stored sentence, the share strip with its accessible values, the bias note and the profitability line', async () => {
    await renderPanel(chanceReadings());
    expect(screen.getByText(/at least as good as the current settings’ in 96\.2% of paths/i)).toBeTruthy();
    const strip = screen.getByRole('img', { name: /Share of 10,000 paired resamples/i });
    expect(strip).toBeTruthy();
    const legend = screen.getByRole('list', { name: 'Paired resample outcomes' });
    expect(legend.textContent).toContain('Better');
    expect(legend.textContent).toContain('Tied');
    expect(legend.textContent).toContain('Worse');
    expect(legend.textContent).toContain('Undefined');
    expect(screen.getByText(/selected on these same development sessions/i)).toBeTruthy();
    expect(screen.getByText(/positive in 97\.4% of 10,000 paths/i)).toBeTruthy();
  });

  it('marks incomplete support when undefined paths exist', async () => {
    await renderPanel(
      chanceReadings({
        comparison: chanceComparison({ incomplete_support: true, counts: { better: 9600, tied: 0, worse: 100, undefined: 300 } }),
      }),
    );
    expect(screen.getByText(/300 of 10,000 paths left the comparison undefined/i)).toBeTruthy();
  });

  it('says same settings plainly instead of drawing strong-looking evidence', async () => {
    await renderPanel({
      comparison: { status: 'same_settings', sentence: 'These are the current settings themselves; there is nothing to compare them with.', basis: chanceComparison().basis },
      profitability: chanceReadings().profitability,
    });
    expect(screen.getByText('These are the current settings themselves; there is nothing to compare them with.')).toBeTruthy();
    expect(screen.queryByRole('img', { name: /Share of/i })).toBeNull();
  });

  it('reads the recorded reason when the comparison was never measured', async () => {
    await renderPanel({
      comparison: { status: 'missing', code: 'not_measured', reason: 'comparison readings were first recorded with #3011' },
      profitability: { status: 'missing', code: 'not_measured', reason: 'Not measured for this study.' },
    });
    expect(screen.getByText(/comparison readings were first recorded with #3011/i)).toBeTruthy();
    expect(screen.getByText('Not measured for this study.')).toBeTruthy();
  });

  it('surfaces a diagnostic failure loudly, never silently', async () => {
    await renderPanel({
      comparison: { status: 'failed', reason: 'RuntimeError: chance computation exploded' },
      profitability: { status: 'failed', reason: 'RuntimeError: chance computation exploded' },
    });
    expect(screen.getByRole('alert').textContent).toContain('chance computation exploded');
  });

  it('passes an accessibility audit in its measured state', async () => {
    const view = await renderPanel(chanceReadings());
    const results = await axe.run(view.fixture.nativeElement, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
