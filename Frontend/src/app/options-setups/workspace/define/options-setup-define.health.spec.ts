import { screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { fakeScopedHealth, healthItem, projection } from '../../../shared/scoped-health/testing/scoped-health-fixtures';
import { renderDefine } from '../../testing/render-workspace';

describe('what Define says about the health of the archive it previews against', () => {
  it('asks about archived option history and its recovery, and links what it finds to the Option archive card', async () => {
    const health = fakeScopedHealth(
      projection(
        healthItem({
          kind: 'option_archive',
          instrument: 'QQQ',
          severity: 'critical',
          explanation: 'QQQ: capture is failing.',
          link: { tab: 'option-archive', query: {}, fragment: 'option-archive-QQQ' },
        }),
      ),
    );

    await renderDefine(fakeService(vi), undefined, { providers: [health.provider] });

    expect(await screen.findByText(/QQQ: capture is failing/)).toBeTruthy();
    expect(health.read).toHaveBeenCalledWith({ operations: ['options_history', 'recovery'], instrument: null });
    expect(screen.getByRole('link', { name: 'Open Option archive' }).getAttribute('href')).toBe('/operations/option-archive#option-archive-QQQ');
    expect(screen.getByRole('heading', { name: 'Entry settings' })).toBeTruthy();
  });
});
