import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { AlpacaSurfaceChooserComponent } from './alpaca-surface-chooser.component';

describe('AlpacaSurfaceChooserComponent', () => {
  it('renders the bots chooser over the lane directory, choosing nothing', async () => {
    await render(AlpacaSurfaceChooserComponent, {
      inputs: { surface: 'bots' },
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    expect(screen.getByRole('heading', { name: 'Alpaca bots' })).toBeTruthy();
    expect(screen.getByText(/no lane is selected automatically/i)).toBeTruthy();
    // The directory's lane list is what chooses; the chooser adds no
    // selection state of its own.
    expect(screen.getByRole('link', { name: 'Gallery lanes' }).getAttribute('href')).toBe(
      '/brokers/alpaca/gallery',
    );
    expect(
      screen.getAllByRole('link', { name: 'Accounts & activity' }).some((link) => link.getAttribute('href') === '/brokers/alpaca'),
    ).toBe(true);
  });

  it('renders the gallery chooser with its sibling link back to bots', async () => {
    await render(AlpacaSurfaceChooserComponent, {
      inputs: { surface: 'gallery' },
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    expect(screen.getByRole('heading', { name: 'Alpaca gallery' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Bots lanes' }).getAttribute('href')).toBe(
      '/brokers/alpaca/bots',
    );
  });

  it('lists every registered lane through the directory', async () => {
    const healthy = testLane();
    const shadow = testLane({
      clerk_id: 'clrk-shadow',
      display_label: 'Live',
      provider_summary: {
        ...healthy.provider_summary,
        confirmed_account_id: '318420190',
        authority_state: 'shadow',
      },
    });
    await render(AlpacaSurfaceChooserComponent, {
      inputs: { surface: 'bots' },
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [healthy, shadow] }),
      ],
    });

    // The lane label renders once per card; "Paper"/"Live" also appear as
    // endpoint-mode facts, so assert on the label elements.
    const labels = screen.getAllByText(/^(Paper|Live)$/).map(
      (element) => element.textContent,
    );
    expect(labels).toContain('Paper');
    expect(labels).toContain('Live');
    expect(screen.getByText('Shadow')).toBeTruthy();
    expect(screen.getByText('2 registered')).toBeTruthy();
  });
});
