import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { AlpacaSurfaceNotReadyTabComponent } from './alpaca-surface-not-ready-tab.component';

describe('AlpacaSurfaceNotReadyTabComponent', () => {
  it('explains a lane that is not ready, and keeps configuration reachable', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-offline', surface: 'bots' },
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [
            testLane({
              clerk_id: 'clerk-offline',
              display_label: 'Live',
              lifecycle_state: 'starting',
            }),
          ],
        }),
      ],
    });

    expect(screen.getByText(/Bots roster unavailable/i)).toBeTruthy();
    expect(screen.getByText(/This lane is Starting,/i)).toBeTruthy();
    expect(screen.getByText(/no other lane is substituted/i)).toBeTruthy();
    expect(
      screen.getByRole('link', { name: 'Open lane configuration' }).getAttribute('href'),
    ).toBe('/brokers/alpaca/clerks/clerk-offline/configuration');
    // It used to be a standalone page with its own "back to the chooser"
    // navigation. Inside the workspace the header and the tab strip are that
    // navigation, so the only link this tab adds is the way forward.
    expect(screen.getAllByRole('link')).toHaveLength(1);
  });

  it('explains a ready lane with no confirmed account binding', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-unbound', surface: 'gallery' },
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [
            testLane({
              clerk_id: 'clerk-unbound',
              provider_summary: { ...testLane().provider_summary, confirmed_account_id: null },
            }),
          ],
        }),
      ],
    });

    expect(screen.getByText(/no confirmed account binding yet/i)).toBeTruthy();
    expect(
      screen.getByRole('link', { name: 'Open lane configuration' }).getAttribute('href'),
    ).toBe('/brokers/alpaca/clerks/clerk-unbound/configuration');
  });

  it('explains a lane without the surface capability, through receiptLabel', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-incapable', surface: 'bots' },
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [
            testLane({
              clerk_id: 'clerk-incapable',
              capabilities: ['account_read', 'configuration_manage', 'gallery_read'],
            }),
          ],
        }),
      ],
    });

    expect(screen.getByText(/Bot Panel Read/i)).toBeTruthy();
    expect(screen.getByText(/capability,/i)).toBeTruthy();
  });

  it('links a lane that became servable to its canonical URL instead of refusing', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-ready', surface: 'bots' },
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane({ clerk_id: 'clerk-ready' })],
        }),
      ],
    });

    expect(screen.getByText(/This lane now serves its Bots roster/i)).toBeTruthy();
    expect(
      screen.getByRole('link', { name: 'Open the Bots roster' }).getAttribute('href'),
    ).toContain('/accounts/');
  });

  it('says so in place when the directory does not list the clerk', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-unknown', surface: 'bots' },
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    expect(screen.getByText(/does not list clerk lane clerk-unknown/i)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open lane configuration' })).toBeNull();
  });
});
