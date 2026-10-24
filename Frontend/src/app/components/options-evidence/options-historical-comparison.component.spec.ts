import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';

import { OptionsHistoricalComparisonComponent } from './options-historical-comparison.component';

describe('Explicit historical comparator', () => {
  it('accepts only a different safe saved run ID and preserves the source and bot return in its link', async () => {
    await render(OptionsHistoricalComparisonComponent, {
      inputs: { sourceRunId: 76, route: ['/options-setups/evidence', 'run', '76'], context: { inspected_study_id: 'os_' + 'a'.repeat(40), bot_return: '/brokers/alpaca/clerks/paper/accounts/PA9/bots/options-1' } },
      providers: [provideRouter([])],
    });
    const field = screen.getByRole('textbox', { name: 'Saved comparator run ID' });
    for (const invalid of ['76', '0', '1.2', '9007199254740992']) {
      await userEvent.clear(field);
      await userEvent.type(field, invalid);
      expect(screen.queryByRole('link', { name: 'Compare selected run' })).toBeNull();
    }
    await userEvent.clear(field);
    await userEvent.type(field, '69');
    const href = screen.getByRole('link', { name: 'Compare selected run' }).getAttribute('href');
    expect(href).toContain('/options-setups/evidence/run/76');
    expect(href).toContain('comparator_run_id=69');
    expect(href).toContain('bot_return=');
    expect(href).toContain('inspected_study_id=');
    expect(href).toContain('view=comparison');
  });
});
