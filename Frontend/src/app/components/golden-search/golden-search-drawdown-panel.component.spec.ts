import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchDrawdownPanelComponent } from './golden-search-drawdown-panel.component';
import { chanceDrawdown } from './testing/fixtures';
import { fakeCharts } from './testing/fake-charts';
import type { CandidateRef, ChanceDrawdown } from './golden-search.types';

const CANDIDATE: CandidateRef = { key: 'all_period', label: 'All-period fit' };

async function renderPanel(drawdown: ChanceDrawdown, candidate: CandidateRef = CANDIDATE) {
  return render(GoldenSearchDrawdownPanelComponent, {
    inputs: { candidate, drawdown },
    providers: fakeCharts().providers,
  });
}

describe('GoldenSearchDrawdownPanelComponent', () => {
  it('shows the stored sentence, the range chart and every marker in its legend', async () => {
    await renderPanel(chanceDrawdown());
    expect(screen.getByText(/95th percentile of resampled session-close falls is 15\.8%, within the 20% ceiling/i)).toBeTruthy();
    expect(screen.getByRole('img', { name: /drawdown range/i })).toBeTruthy();
    expect(screen.getByText(/Recorded, engine's every bar: 16\.4%/i)).toBeTruthy();
    expect(screen.getByRole('list', { name: 'Drawdown markers' }).textContent).toContain('Frozen ceiling');
  });

  it('reads not recorded for a missing engine figure, never zero', async () => {
    await renderPanel(chanceDrawdown({ recorded_engine_per_bar: null }));
    expect(screen.getByText(/Recorded, engine's every bar: not recorded/i)).toBeTruthy();
  });

  it('carries the no-sizing-advice caveat and the peak-convention caveat', async () => {
    await renderPanel(chanceDrawdown());
    expect(screen.getByText(/recommends no capital or position size/i)).toBeTruthy();
    expect(screen.getByText(/neither bounds the other/i)).toBeTruthy();
  });

  it('reads the recorded reason when the drawdown was never measured', async () => {
    await renderPanel({ status: 'missing', code: 'not_measured', reason: 'Not measured for this study: drawdown readings were first recorded with #3013.' });
    expect(screen.getByText(/first recorded with #3013/i)).toBeTruthy();
  });

  it('surfaces a diagnostic failure loudly from its stored sentence', async () => {
    await renderPanel({ status: 'failed', reason: 'RuntimeError: drawdown exploded', sentence: 'The drawdown reading could not be computed and nothing was stored: RuntimeError: drawdown exploded.' });
    expect(screen.getByRole('alert').textContent).toContain('drawdown exploded');
  });

  it('passes an accessibility audit in its measured state', async () => {
    const view = await renderPanel(chanceDrawdown());
    const results = await axe.run(view.fixture.nativeElement, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
