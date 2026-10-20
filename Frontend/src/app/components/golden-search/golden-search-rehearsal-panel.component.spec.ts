import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchRehearsalPanelComponent } from './golden-search-rehearsal-panel.component';
import { chanceRehearsal } from './testing/fixtures';
import { fakeCharts } from './testing/fake-charts';
import type { CandidateRef, ChanceRehearsal } from './golden-search.types';

const CANDIDATE: CandidateRef = { key: 'all_period', label: 'All-period fit' };

async function renderPanel(rehearsal: ChanceRehearsal, candidate: CandidateRef = CANDIDATE) {
  return render(GoldenSearchRehearsalPanelComponent, {
    inputs: { candidate, rehearsal },
    providers: fakeCharts().providers,
  });
}

describe('GoldenSearchRehearsalPanelComponent', () => {
  it('renders a measured rehearsal: the stored guidance, the joint ladder with its target line, the hypothetical-length caveat, no claim the test is too short, and an accessibility audit', async () => {
    const view = await renderPanel(chanceRehearsal());
    expect.soft(screen.getByText(/61% of resamples meet every modeled rule/i), 'shows the stored guidance').toBeTruthy();
    expect.soft(screen.getByText(/2× rung reaches the 80% target/i), 'shows the joint ladder with its target line').toBeTruthy();
    expect.soft(screen.getByRole('img', { name: /final-test rehearsal/i }), 'draws every active rule as its own series').toBeTruthy();

    const caption = screen.getByText(/not a validated date range: a revision that changes the interval starts a new study/i);
    expect.soft(caption, 'keeps the hypothetical-length caveat visible').toBeTruthy();
    expect.soft(document.body.textContent, 'never says the test is too short').not.toContain('test is too short');
    expect.soft(document.body.textContent, 'never says anything is too short').not.toContain('too short');

    const results = await axe.run(view.fixture.nativeElement, { rules: { 'color-contrast': { enabled: false } } });
    expect.soft(results.violations.map((v) => v.id), 'passes an accessibility audit in its measured state').toEqual([]);
  });

  it('reads the recorded reason when the rehearsal was never measured or is unavailable', async () => {
    await renderPanel({ status: 'missing', code: 'not_measured', reason: 'Not measured for this study: final-test rehearsals were first recorded with #3012.' });
    expect(screen.getByText(/first recorded with #3012/i)).toBeTruthy();
  });

  it('surfaces a diagnostic failure loudly from its stored sentence', async () => {
    await renderPanel({
      status: 'failed',
      reason: 'RuntimeError: rehearsal exploded',
      sentence: 'The rehearsal reading could not be computed and nothing was stored: RuntimeError: rehearsal exploded.',
    });
    expect(screen.getByRole('alert').textContent).toContain('rehearsal exploded');
  });
});
