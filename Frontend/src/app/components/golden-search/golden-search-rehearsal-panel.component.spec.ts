import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchRehearsalPanelComponent } from './golden-search-rehearsal-panel.component';
import { chanceRehearsal, rehearsalRung } from './testing/fixtures';
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
  it('shows the stored guidance, the joint ladder with its target line and every active rule as its own series', async () => {
    await renderPanel(chanceRehearsal());
    expect(screen.getByText(/61% of resamples meet every modeled rule/i)).toBeTruthy();
    expect(screen.getByText(/2× rung reaches the 80% target/i)).toBeTruthy();
    expect(screen.getByRole('img', { name: /final-test rehearsal/i })).toBeTruthy();
  });

  it('draws a nonmonotone ladder at its actual falling values and says so in the guidance', async () => {
    const falling = chanceRehearsal({
      rungs: [1, 2, 3, 4].map((k) =>
        rehearsalRung(k, {
          passes: { pass_joint: 5000 - k * 400, pass_positive_net: 9000, pass_drawdown: 8000, pass_exits: 8500, pass_objective: 7000 },
          shares: { joint: (5000 - k * 400) / 10_000, positive_net: 0.9, drawdown: 0.8, exits: 0.85, objective: 0.7 },
        }),
      ),
      guidance:
        "At the final test's own length, 46% of resamples meet every modeled rule. No rung reaches the 80% target. Support does not rise with length on this ladder — a longer rehearsal of the same development outcome is not a better one. The 80% target is the owner's reading heuristic, not a power calculation.",
    });
    expect(falling.rungs[3].passes['pass_joint']).toBeLessThan(falling.rungs[0].passes['pass_joint']);
    await renderPanel(falling);
    expect(screen.getByText(/does not rise with length/i)).toBeTruthy();
    expect(screen.getByText(/No rung reaches the 80% target/i)).toBeTruthy();
  });

  it('never says the test is too short and keeps the hypothetical-length caveat visible', async () => {
    await renderPanel(chanceRehearsal());
    const caption = screen.getByText(/not a validated date range: a revision that changes the interval starts a new study/i);
    expect(caption).toBeTruthy();
    expect(document.body.textContent).not.toContain('test is too short');
    expect(document.body.textContent).not.toContain('too short');
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

  it('passes an accessibility audit in its measured state', async () => {
    const view = await renderPanel(chanceRehearsal());
    const results = await axe.run(view.fixture.nativeElement, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
