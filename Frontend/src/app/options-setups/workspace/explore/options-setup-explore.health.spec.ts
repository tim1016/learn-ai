import { screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { EXAMPLE_ONE_REENTRY } from '../../../components/options-backtest/testing/fixtures';
import { fakeScopedHealth, healthItem, projection } from '../../../shared/scoped-health/testing/scoped-health-fixtures';
import { renderExplore } from '../../testing/render-workspace';

describe('what Explore says about the health of the data it replays', () => {
  it('asks about archived option history and its recovery, and links what it finds to the Option archive card', async () => {
    const health = fakeScopedHealth(
      projection(
        healthItem({
          kind: 'option_archive',
          instrument: 'SPY',
          severity: 'warning',
          explanation: 'SPY: Archive gaps need attention before their vendor deadline.',
          link: { tab: 'option-archive', query: {}, fragment: 'option-archive-SPY' },
        }),
      ),
    );

    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { providers: [health.provider] });

    const notice = await screen.findByText(/Archive gaps need attention/);
    expect(health.read).toHaveBeenCalledWith({ operations: ['options_history', 'recovery'], instrument: null });
    expect(notice.closest('.health-notice')?.textContent).toContain('Your requested sessions are checked separately by Check data.');
    expect(screen.getByRole('link', { name: 'Open Option archive' }).getAttribute('href')).toBe('/operations/option-archive#option-archive-SPY');
    // The notice is beside the page, not in front of it.
    expect(screen.getByRole('heading', { name: 'What this backtest tests' })).toBeTruthy();
  });

  it('shows a failed backup as a backup problem, and nothing at all when what it replays is healthy', async () => {
    const down = fakeScopedHealth(
      projection(healthItem({ kind: 'lake_backup', severity: 'critical', explanation: 'The destination is unavailable.', link: { tab: 'backups', query: {}, fragment: 'option-archive-backup' } })),
    );

    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { providers: [down.provider] });

    expect(await screen.findByRole('link', { name: 'Open Backups' })).toBeTruthy();
  });

  it('shows no notice when the data plane has nothing to say', async () => {
    const quiet = fakeScopedHealth(projection(healthItem({ kind: 'option_archive', instrument: 'SPY' })));

    const view = await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { providers: [quiet.provider] });
    await waitFor(() => expect(quiet.read).toHaveBeenCalledOnce());
    await view.fixture.whenStable();

    expect(view.container.querySelector('app-health-notice .health-notice')).toBeNull();
  });
});
