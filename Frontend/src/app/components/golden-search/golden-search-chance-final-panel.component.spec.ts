import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchChanceFinalPanelComponent } from './golden-search-chance-final-panel.component';
import { chanceCarryOver, chanceFitToFitRecentDirection, finalChance } from './testing/fixtures';
import { fakeCharts } from './testing/fake-charts';
import type { CandidateRef, FinalChance } from './golden-search.types';

const CANDIDATE: CandidateRef = { key: 'all_period', label: 'All-period fit' };
const RECENT: CandidateRef = { key: 'recent', label: 'Recent fit' };

async function renderPanel(chance: FinalChance, candidate: CandidateRef = CANDIDATE, positiveNetRequired = true) {
  return render(GoldenSearchChanceFinalPanelComponent, {
    inputs: { candidate, chance, positiveNetRequired },
    providers: fakeCharts().providers,
  });
}

describe('GoldenSearchChanceFinalPanelComponent', () => {
  it('renders a measured final chance: the stored comparison, bands and carry-over, the resampled window with its caveats, and the All-period fit’s own direction', async () => {
    await renderPanel(finalChance());
    expect.soft(screen.getByText(/at least as good as the current settings’ in 96\.2% of paths/i), 'shows the stored comparison sentence').toBeTruthy();
    expect.soft(screen.getByRole('img', { name: /final objective uncertainty/i }), 'shows the objective band').toBeTruthy();
    expect.soft(screen.getByRole('img', { name: /final profit uncertainty/i }), 'shows the independent profit band').toBeTruthy();
    expect.soft(screen.getByRole('img', { name: /result among its rehearsals/i }), 'shows the carry-over with its curve').toBeTruthy();
    expect.soft(screen.getByText(/positive in 97\.4% of 10,000 resampled final paths/i), 'shows the money-support sentence').toBeTruthy();
    expect.soft(screen.getByText(/paired resamples of these final sessions/i), 'names the window it resampled').toBeTruthy();
    expect.soft(screen.getByText(/frozen "meets the rules" verdict above is\s+separate from this model support/i), 'keeps the frozen-verdict separation').toBeTruthy();
    expect.soft(screen.getAllByText(/chosen after the final test/i).length, 'carries the two-selection caveat').toBeGreaterThan(0);
    expect.soft(screen.queryByText(/selected on these same development sessions/i), 'never the development note').toBeNull();
    expect.soft(screen.getByText(/All-period fit was at least as good as Recent fit in 68\.4% of paths/i), 'shows the All-period fit its own direction of the shared draw (#3029)').toBeTruthy();
    expect.soft(screen.getByText(/not adjusted for that choice/i), 'says the direction is unadjusted for that choice (#3029)').toBeTruthy();
    expect.soft(screen.queryByText(/Recent fit was at least as good as/i), 'never the other fit’s direction (#3029)').toBeNull();
  });

  it('shows measured profit when the incumbent comparison is unavailable, on its own (#3026)', async () => {
    await renderPanel(
      finalChance({
        comparison: { status: 'missing', code: 'incumbent_unavailable', reason: 'The current settings’ run cannot be read, so no comparison is available; the fit’s own readings are unaffected.' },
      }),
    );
    expect(screen.getByText(/no comparison is available/i)).toBeTruthy();
    expect(screen.getByText(/positive in 97\.4% of 10,000 resampled final paths/i)).toBeTruthy();
    expect(screen.getByRole('img', { name: /final profit uncertainty/i })).toBeTruthy();
    expect(screen.getByRole('img', { name: /result among its rehearsals/i })).toBeTruthy();
    expect(screen.queryByRole('img', { name: /final objective uncertainty/i })).toBeNull();
  });

  it('shows a missing or failed profit reading’s own reason beside a valid comparison (#3026)', async () => {
    await renderPanel(
      finalChance({
        profitability: { status: 'failed', reason: 'ValueError: boom', sentence: 'The profitability reading could not be computed and nothing was stored: ValueError: boom.' },
      }),
    );
    expect(screen.getByText(/at least as good as the current settings’ in 96\.2% of paths/i)).toBeTruthy();
    expect(screen.getByRole('img', { name: /final objective uncertainty/i })).toBeTruthy();
    const alert = screen.getAllByRole('alert')[0];
    expect(alert.textContent).toContain('profitability reading could not be computed');
  });

  it('omits the money-support row entirely when the frozen plan does not require a positive net (#3026)', async () => {
    await renderPanel(finalChance(), CANDIDATE, false);
    expect(screen.queryByText(/positive in 97\.4% of 10,000 resampled final paths/i)).toBeNull();
    expect(screen.queryByRole('img', { name: /final profit uncertainty/i })).toBeNull();
    expect(screen.queryByText(/Money support/i)).toBeNull();
    // The objective comparison and the carry-over stay.
    expect(screen.getByText(/at least as good as the current settings’ in 96\.2% of paths/i)).toBeTruthy();
    expect(screen.getByRole('img', { name: /result among its rehearsals/i })).toBeTruthy();
  });

  it('explains that a stored reading from before the range simply did not record one (#3026)', async () => {
    const before = finalChance();
    const legacy = { ...before.profitability } as typeof before.profitability;
    if (legacy.status === 'measured') delete legacy.net_return_range;
    await renderPanel(finalChance({ profitability: legacy }));
    expect(screen.getByText(/predates the compounded-net range/i)).toBeTruthy();
    expect(screen.getByText(/positive in 97\.4% of 10,000 resampled final paths/i)).toBeTruthy();
    expect(screen.queryByRole('img', { name: /final profit uncertainty/i })).toBeNull();
  });

  it('announces a failed fit-to-fit as an alert instead of rendering nothing', async () => {
    await renderPanel(
      finalChance({
        fit_to_fit: {
          status: 'failed',
          reason: 'KeyError: label',
          sentence: 'The fit-to-fit reading could not be computed and nothing was stored: KeyError: label.',
        },
      }),
    );
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('could not be computed');
  });

  it('shows the Recent fit its own direction when it is the fit the page is about (#3029)', async () => {
    await renderPanel(finalChance({ fit_to_fit: chanceFitToFitRecentDirection() }), RECENT);
    expect(screen.getByText(/Recent fit was at least as good as All-period fit in 34\.0% of paths/i)).toBeTruthy();
    expect(screen.queryByText(/All-period fit was at least as good as Recent fit in 68\.4%/i)).toBeNull();
  });

  it('says so in the server’s words when this fit has no recorded direction (#3029)', async () => {
    await renderPanel(
      finalChance({
        fit_to_fit: {
          status: 'missing',
          code: 'direction_unavailable',
          reason:
            'The stored fit-to-fit record predates both-direction summaries (#3029): it recorded only the All-period fit’s direction, and this fit’s own direction was never recorded. It is not recomputed on read.',
        },
      }),
    );
    expect(screen.getByText(/recorded only the All-period fit’s direction/i)).toBeTruthy();
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
      finalChance({
        comparison: { status: 'missing', code: 'not_measured', reason: 'Not measured for this study.' },
        profitability: { status: 'missing', code: 'not_measured', reason: 'Not measured for this study.' },
        carry_over: { status: 'missing', code: 'carry_over_unavailable', reason: 'No matching stored rehearsal could carry over.' },
        fit_to_fit: { status: 'missing', code: 'single_fit', reason: 'Only one distinct fit sat this final test.' },
      }),
    );
    expect(screen.getByText(/No final uncertainty was measured for this fit/i)).toBeTruthy();
  });

  it('shows the server’s own words for a missing comparison and a failed carry-over, never silence', async () => {
    await renderPanel(
      finalChance({
        comparison: { status: 'missing', code: 'not_measured', reason: 'Not measured for this study: final comparison readings were first recorded with #3014.' },
        profitability: { status: 'missing', code: 'not_measured', reason: 'Not measured for this study.' },
        carry_over: {
          status: 'failed',
          reason: 'ValueError: boom',
          sentence: 'The carry over reading could not be computed and nothing was stored: ValueError: boom.',
        },
      }),
    );
    expect(screen.getByText(/first recorded with #3014/i)).toBeTruthy();
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('could not be computed');
    expect(screen.queryByText(/No final uncertainty was measured for this fit/i)).toBeNull();
  });

  it('renders an invalid stored comparison’s server reason beside still-valid carry-over evidence (#3024)', async () => {
    await renderPanel(
      finalChance({
        comparison: {
          status: 'missing',
          code: 'invalid_record',
          reason:
            'The stored comparison reading fails validation and is shown as unavailable rather than guessed at: its schema version \'golden-search-chance-comparison/v999\' is not a supported comparison schema.',
        },
      }),
    );
    expect(screen.getByText(/fails validation and is shown as unavailable/i)).toBeTruthy();
    expect(screen.getByText(/v999/i)).toBeTruthy();
    // The malformed sibling hides nothing: the measured carry-over chart still shows.
    expect(screen.getByRole('img', { name: /result among its rehearsals/i })).toBeTruthy();
  });

  it('passes an accessibility audit in its measured state', async () => {
    const view = await renderPanel(finalChance());
    const results = await axe.run(view.fixture.nativeElement, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
