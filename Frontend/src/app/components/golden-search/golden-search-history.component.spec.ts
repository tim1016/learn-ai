import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { GoldenSearchHistoryComponent } from './golden-search-history.component';
import { GoldenSearchService } from './golden-search.service';
import type { StudyListFilters, StudySummary } from './golden-search.types';
import { emaCapability, studyDetail } from './testing/fixtures';

function summary(overrides: Partial<StudySummary> = {}): StudySummary {
  const { protocol: _p, receipt: _r, permitted_actions: _a, action_refusals: _ar, guidance: _g, progress: _pr, dispatch: _d, results: _res, decision: _dec, candidate_key: _c, exam_locked: _e, ...row } = studyDetail('awaiting_candidate');
  return { ...row, ...overrides };
}

describe('GoldenSearchHistoryComponent', () => {
  it('lists each study with enough to judge it, and opens it on its own page', async () => {
    const list = vi.fn(async (_filters: StudyListFilters) => [
      summary(),
      summary({ id: 'study-0002-bbbb', state: 'approved', exam_outcome: 'meets_rules', exposure_claim: 'confirmatory', hidden: true }),
      summary({ id: 'study-0003-cccc', state: 'exam_running', presented_status: 'running', exposure_claim: 'exploratory' }),
    ]);
    await render(GoldenSearchHistoryComponent, {
      inputs: { capabilities: [emaCapability()] },
      providers: [provideRouter([]), ...fakePickerWorld().providers, { provide: GoldenSearchService, useValue: { list } }],
    });

    const rows = await screen.findAllByRole('row');
    expect(rows[1].textContent).toContain('EMA Crossover Signal');
    expect(rows[1].textContent).toContain('Awaiting Candidate');
    expect(rows[1].textContent).toContain('420 / 5000');
    expect(rows[1].textContent).toContain('Held back');
    expect(rows[2].textContent).toMatch(/Meets Rules · confirmatory/);
    expect(rows[2].textContent).toContain('Hidden');
    expect(within(rows[2]).getByRole('link', { name: /open study study-00/i }).getAttribute('href')).toBe('/golden-search/study-0002-bbbb');
    expect(rows[3].textContent).toMatch(/Opened · exploratory/);
    expect(rows[3].textContent).not.toContain('Held back');
  });

  it('asks the server again for each filter, hidden studies only on request', async () => {
    const list = vi.fn(async (_filters: StudyListFilters) => [summary()]);
    await render(GoldenSearchHistoryComponent, {
      inputs: { capabilities: [emaCapability()] },
      providers: [provideRouter([]), ...fakePickerWorld().providers, { provide: GoldenSearchService, useValue: { list } }],
    });
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByRole('combobox', { name: /filter by strategy/i }), { target: { value: 'ema_crossover_signal' } });
    await waitFor(() => expect(list.mock.lastCall?.[0]).toMatchObject({ strategy_key: 'ema_crossover_signal' }));
    expect(list.mock.lastCall?.[0].include_hidden).toBeUndefined();

    fireEvent.click(screen.getByRole('checkbox', { name: /show hidden/i }));
    await waitFor(() => expect(list.mock.lastCall?.[0]).toMatchObject({ strategy_key: 'ema_crossover_signal', include_hidden: true }));
  });
});
