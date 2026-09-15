import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import { AlpacaLaneDirectoryComponent } from './alpaca-lane-directory.component';

describe('AlpacaLaneDirectoryComponent', () => {
  it('keeps healthy lane actions available while making a failed lane explicit', async () => {
    const healthy = testLane();
    const failed = testLane({
      clerk_id: 'clerk-offline',
      display_label: 'Live',
      lifecycle_state: 'unreachable',
      provider_summary: {
        ...healthy.provider_summary,
        confirmed_account_id: 'live-account',
      },
    });
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [healthy, failed] }),
      ],
    });

    expect(screen.getAllByText('Paper')).not.toHaveLength(0);
    expect(screen.getByText('Live')).not.toBeNull();
    expect(screen.getByText(/other healthy lanes remain available/i)).not.toBeNull();
    expect(screen.getAllByRole('link', { name: 'Desk' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Bots' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Gallery' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Deploy' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Configuration' })).toHaveLength(2);
  });

  it('does not advertise lane surfaces whose provider capability is absent', async () => {
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane({ capabilities: ['account_read'] })],
        }),
      ],
    });

    expect(screen.getByRole('link', { name: 'Desk' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Bots' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Gallery' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Deploy' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Configuration' })).toBeNull();
  });

  it("renders the lane's authority state through receiptLabel (#2102)", async () => {
    // `authority_state` (records.py) reaches the browser but rendered nowhere
    // before #2102 — it is a raw backend identifier (like `endpoint_mode`
    // above), so it belongs through the shared pipe, not as literal prose.
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane({ provider_summary: { authority_state: 'real_live' } })],
        }),
      ],
    });

    expect(screen.getByText('Real Live')).toBeTruthy();
  });

  it('makes a global Deploy intent an explicit lane-selection step', async () => {
    await render(AlpacaLaneDirectoryComponent, {
      inputs: { requestedSurface: 'deploy' },
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    expect(screen.getByText(/choose a ready clerk lane below to deploy/i)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Deploy' }).getAttribute('href')).toContain(
      '/brokers/alpaca/clerks/',
    );
  });
});
