import { fireEvent, screen, waitFor } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../shared/date/et-midnight';
import { sessionOpenMs } from '../../shared/date-picker/testing/fake-trading-calendar';
import { dataCheck, sessionCheck } from './testing/fixtures';
import { checkButton, openExample, pickDates } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

async function violations(): Promise<string[]> {
  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  return results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`);
}

describe('the run part of the page for a screen reader', () => {
  it('has no accessibility violations with the dates picked and every part of a Check data answer showing', async () => {
    const answer = dataCheck(
      [
        sessionCheck('2026-09-14', { verdict: 'cannot_be_tested', problems: [{ code: 'older_than_the_data_plan', message: 'Older than the plan.', lake_reason: null }], notes: ['A note.'] }),
        sessionCheck('2026-09-15', { verdict: 'needs_download', missing: ['underlying_bars', 'contracts'], contracts: { state: 'known', needed: 9, missing: 4 } }),
        sessionCheck('2026-09-16', { skip: 'window_does_not_fit' }),
        sessionCheck('2026-09-17'),
      ],
      {
        longest_testable_stretch: {
          start_ms: etMidnightMs('2026-09-15'),
          end_ms: etMidnightMs('2026-09-18'),
          first_session_open_ms: sessionOpenMs('2026-09-15'),
          last_session_open_ms: sessionOpenMs('2026-09-17'),
          sessions: 3,
        },
      },
    );
    await renderPage(fakeService(vi, { checkData: vi.fn(async () => answer) }));
    await openExample();
    pickDates('2026-09-14', '2026-09-17');
    await waitFor(() => expect(checkButton().disabled).toBe(false));

    fireEvent.click(checkButton());
    await screen.findByRole('button', { name: 'Run available dates' });

    expect(await violations()).toEqual([]);
  });
});
