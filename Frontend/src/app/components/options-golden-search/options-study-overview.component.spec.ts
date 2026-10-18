import { fireEvent, render, screen, within } from '@testing-library/angular';
import { expect, it } from 'vitest';

import { OptionsStudyOverviewComponent } from './options-study-overview.component';
import { candidate, study } from './testing';

it('compares exact profit and risk, keeping final-window outcomes separate', async () => {
  const original = candidate({ is_original: true, label: 'Original', candidate_hash: 'seed', net_profit_usd: '9007199254740993.01' });
  const view = await render(OptionsStudyOverviewComponent, { inputs: { study: study({ candidates: [original, candidate()], final_results: [candidate({ candidate_hash: 'seed', net_profit_usd: '-25.0123456789' })] }) } });
  expect(view.container.querySelectorAll('circle')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: 'Original' }));
  expect(view.container.querySelector('.selected-reading')?.textContent).toContain('9007199254740993.01 USD net profit');
  const row = screen.getByRole('row', { name: /Original Original/ });
  expect(within(row).getByRole('cell', { name: '-25.0123456789' })).toBeTruthy();
});

it('excludes technically unverifiable totals from the comparison plot and explains failed folds', async () => {
  const partial = candidate({ verdict: 'could_not_evaluate', net_profit_usd: '1234.56789' });
  const gate = { gate_id: 'forward_0:selection', status: 'does_not_meet_rules', actual: null, required: 'eligible training winner', reason: 'No eligible training winner.' };
  const view = await render(OptionsStudyOverviewComponent, { inputs: { study: study({ candidates: [partial], gate_outcomes: [gate] }) } });
  expect(view.container.querySelector('circle')).toBeNull();
  expect(screen.getByText(/No technically verified candidate/)).toBeTruthy();
  expect(screen.getByRole('cell', { name: '1234.56789 Partial diagnostics' })).toBeTruthy();
  expect(screen.getByRole('rowheader', { name: 'Forward 0 · Selection' })).toBeTruthy();
  expect(screen.getByRole('cell', { name: 'No eligible training winner.' })).toBeTruthy();
});
