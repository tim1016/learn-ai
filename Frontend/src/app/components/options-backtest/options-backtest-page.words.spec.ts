import { screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SetupSummary } from './options-backtest.types';
import { addLeg, choose, legCard } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

/** The words the page uses where a name alone would mislead. Each test pins a fact a hint states, never a whole sentence. */

const describedBy = (element: HTMLElement): string => document.getElementById(element.getAttribute('aria-describedby') ?? '')?.textContent ?? '';

describe('the expiry choices', () => {
  it('call the weekly expiry Weekly, and a leg built on it says Weekly expiry', async () => {
    await renderPage(fakeService(vi));

    expect(screen.getByRole('radio', { name: 'Weekly' })).toBeTruthy();
    expect(screen.queryByRole('radio', { name: /this week/i })).toBeNull();
    addLeg();

    expect(legCard(1).textContent).toContain('Weekly expiry');
    expect(legCard(1).textContent).not.toMatch(/expires this week/i);
  });

  it('say, tied to the pills, what the chosen expiry is, and the hint follows the choice', async () => {
    await renderPage(fakeService(vi));
    const pills = screen.getByRole('group', { name: 'Expiry' });

    expect(describedBy(pills)).toMatch(/last trading day of its week/);
    expect(describedBy(pills)).toMatch(/today included/);
    expect(describedBy(pills)).toMatch(/later week/);

    choose('Same day');
    expect(describedBy(pills)).toMatch(/trading date itself/);

    choose('At least N days');
    expect(describedBy(pills)).toMatch(/calendar days/);
  });
});

describe('the Instrument card', () => {
  it('does not claim the picker checks for standard options, and says what is true of the underlying', async () => {
    await renderPage(fakeService(vi));

    const text = within(screen.getByRole('region', { name: 'Instrument' })).getByText(/raw, unadjusted price/).textContent ?? '';

    expect(text).not.toMatch(/standard options/i);
    expect(text).toMatch(/downloaded first/i);
    expect(text).toMatch(/when a backtest is run/i);
  });
});

describe('Export', () => {
  const exportButton = () => screen.getByRole('button', { name: 'Export setup' }) as HTMLButtonElement;

  it('says it needs a complete setup while there is none, tied to the button and on hover', async () => {
    await renderPage(fakeService(vi));

    expect(exportButton().disabled).toBe(true);
    expect(describedBy(exportButton())).toMatch(/setup is complete/i);
    expect(exportButton().getAttribute('title')).toBe(describedBy(exportButton()));
  });

  it('says it is waiting for the server to read the setup back while a complete setup is not read back yet', async () => {
    const service = fakeService(vi, { describe: vi.fn(() => new Promise<SetupSummary>(() => undefined)) });
    await renderPage(service);
    addLeg();

    await waitFor(() => expect(describedBy(exportButton())).toMatch(/read the setup back/i));
    expect(exportButton().disabled).toBe(true);
  });

  it('says the same when the server could not read the setup back', async () => {
    await renderPage(fakeService(vi, { describe: vi.fn(async () => Promise.reject(new Error('offline'))) }));
    addLeg();

    await waitFor(() => expect(describedBy(exportButton())).toMatch(/read the setup back/i));
  });

  it('gives no reason once the server has read the setup back, and is enabled', async () => {
    await renderPage(fakeService(vi));
    addLeg();

    await waitFor(() => expect(exportButton().disabled).toBe(false));
    expect(exportButton().getAttribute('aria-describedby')).toBeNull();
    expect(exportButton().getAttribute('title')).toBeNull();
    expect(document.getElementById('ob-export-why')).toBeNull();
  });
});
