import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { AlpacaSurfaceNotReadyTabComponent } from './alpaca-surface-not-ready-tab.component';

describe('AlpacaSurfaceNotReadyTabComponent', () => {
  it('explains a lane that is not ready, and keeps Settings reachable', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-offline' },
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

    expect(screen.getByRole('heading', { name: 'Home unavailable' })).toBeTruthy();
    expect(screen.getByText(/This lane is Starting,/i)).toBeTruthy();
    expect(screen.getByText(/No other account is shown in its place/i)).toBeTruthy();
    expect(
      screen.getByRole('link', { name: 'Open Settings' }).getAttribute('href'),
    ).toBe('/brokers/alpaca/clerks/clerk-offline/settings');
    // It used to be a standalone page with its own "back to the chooser"
    // navigation. Inside the workspace the header and the tab strip are that
    // navigation, so the only link this tab adds is the way forward.
    expect(screen.getAllByRole('link')).toHaveLength(1);
  });

  it('explains a ready lane with no confirmed account binding', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-unbound' },
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
      screen.getByRole('link', { name: 'Open Settings' }).getAttribute('href'),
    ).toBe('/brokers/alpaca/clerks/clerk-unbound/settings');
  });

  it('links a lane that became servable to its account\'s Home instead of refusing', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-ready' },
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane({ clerk_id: 'clerk-ready' })],
        }),
      ],
    });

    expect(screen.getByText(/This lane now serves its account's Home/i)).toBeTruthy();
    expect(
      screen.getByRole('link', { name: 'Open Home' }).getAttribute('href'),
    ).toBe(`/brokers/alpaca/clerks/clerk-ready/accounts/${testLane().provider_summary?.confirmed_account_id}`);
  });

  it('says so in place when the directory does not list the clerk', async () => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-unknown' },
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    expect(screen.getByText(/does not list clerk lane clerk-unknown/i)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open Settings' })).toBeNull();
  });

  it.each([
    ['a lane that is not ready', testLane({ clerk_id: 'clerk-x', lifecycle_state: 'starting' })],
    ['a lane with no confirmed account', testLane({
      clerk_id: 'clerk-x', provider_summary: { ...testLane().provider_summary, confirmed_account_id: null },
    })],
  ])('has no detectable accessibility violations for %s', async (_name, lane) => {
    await render(AlpacaSurfaceNotReadyTabComponent, {
      inputs: { clerkId: 'clerk-x' },
      providers: [provideRouter([]), provideFleetDirectory({ observed_at_ms: 1, clerks: [lane] })],
    });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });
});
