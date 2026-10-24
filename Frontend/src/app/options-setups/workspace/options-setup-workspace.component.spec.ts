import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SavedSetup } from '../../components/options-backtest/options-backtest.types';
import { fakeService } from '../../components/options-backtest/testing/fake-service';
import { EXAMPLE_NO_REENTRY, exampleBody, servedExamples, servedSaved } from '../../components/options-backtest/testing/fixtures';
import { indexOf, savedName } from '../testing/fixtures';
import { renderTab } from '../testing/render-workspace';

const withLibrary = (...extra: SavedSetup[]) => fakeService(vi, { list: vi.fn(async () => [...servedExamples(), ...extra]) });
const quiet = { index: indexOf({ setups: [savedName('Mine', '1')] }), bots: { kind: 'known' as const, bots: [] } };

describe('a setup that is not in the library', () => {
  it('fails in place, at the address, with a way back, and opens no other setup in its place', async () => {
    await renderTab(withLibrary(servedSaved('Mine', exampleBody())), 'No such setup', 'define', { ...quiet, form: false });

    const notice = await screen.findByRole('alert');
    expect(notice.textContent).toContain('No saved setup is named “No such setup”.');
    expect(notice.textContent).toContain('open by their own ids');
    expect(within(notice).getByRole('link', { name: 'Options setups' }).getAttribute('href')).toBe('/options-setups');
    expect(within(notice).getByRole('link', { name: 'New options setup' }).getAttribute('href')).toBe('/options-setups/_new/define');
    expect(screen.queryByRole('heading', { name: 'Entry settings' })).toBeNull();
    expect(screen.queryByRole('navigation', { name: 'Options setup tabs' })).toBeNull();
    expect(decodeURIComponent(TestBed.inject(Router).url)).toBe('/options-setups/No such setup/define');
  });

  it('says what a saved setup that can no longer be opened could not be read for, and offers no tab to open it in', async () => {
    const broken: SavedSetup = { name: 'Old rules', example: false, saved_at_ms: 1_000, setup: null, cannot_open_reason: 'legs.0.contracts: Input should be at most 1000' };
    await renderTab(withLibrary(broken), 'Old rules', 'overview', { ...quiet, form: false });

    const notice = await screen.findByRole('alert');

    expect(notice.textContent).toContain('“Old rules” is saved but can no longer be opened.');
    expect(notice.textContent).toContain('legs.0.contracts: Input should be at most 1000');
    expect(screen.queryByRole('navigation', { name: 'Options setup tabs' })).toBeNull();
  });

  it('says the saved setups could not be read, and reads them again on request', async () => {
    let fail = true;
    const service = fakeService(vi, {
      list: vi.fn(async () => {
        if (fail) throw new Error('database down');
        return [...servedExamples(), servedSaved('Mine', exampleBody())];
      }),
    });
    await renderTab(service, 'Mine', 'define', { ...quiet, form: false });

    fail = false;
    fireEvent.click(within(await screen.findByRole('alert')).getByRole('button', { name: 'Read again' }));

    await screen.findByRole('heading', { name: 'Entry settings' });
    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Mine'));
  });

  it('says it is reading the saved setups, and shows nothing of a setup, while the library is being read', async () => {
    const service = fakeService(vi, { list: vi.fn(() => new Promise<SavedSetup[]>(() => undefined)) });
    await renderTab(service, 'Mine', 'define', { ...quiet, form: false, settled: false });

    expect(await screen.findByText('Reading the saved setups.')).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: 'Options setup tabs' })).toBeNull();
  });
});

describe('a setup\'s place', () => {
  it('is titled with the setup, says what it is and whether the draft matches what was saved, and leads back to the list', async () => {
    await renderTab(withLibrary(servedSaved('Mine', exampleBody())), 'Mine', 'overview', quiet);

    expect(screen.getByRole('heading', { level: 1, name: 'Mine' })).toBeTruthy();
    await waitFor(() => expect(screen.getByText('Matches saved')).toBeTruthy());
    const trail = screen.getByRole('navigation', { name: 'Breadcrumb' });
    expect(within(trail).getByRole('link', { name: 'Options setups' }).getAttribute('href')).toBe('/options-setups');
  });

  it('marks a shipped example as one', async () => {
    await renderTab(withLibrary(), EXAMPLE_NO_REENTRY, 'overview', quiet);

    await screen.findByText('Example');
    expect(screen.getByText('Shipped example')).toBeTruthy();
  });

  it('declares four tabs once; Overview and Define are links and the open one is marked', async () => {
    await renderTab(withLibrary(servedSaved('Mine', exampleBody())), 'Mine', 'define', quiet);

    const tabs = screen.getByRole('navigation', { name: 'Options setup tabs' });

    expect(within(tabs).getByRole('link', { name: 'Overview' }).getAttribute('href')).toBe('/options-setups/Mine/overview');
    expect(within(tabs).getByRole('link', { name: 'Define' }).getAttribute('aria-current')).toBe('page');
    expect(within(tabs).getByRole('link', { name: 'Overview' }).getAttribute('aria-current')).toBeNull();
  });

  it('keeps Explore and Golden in the strip, greyed and with no address, and says why', async () => {
    await renderTab(withLibrary(servedSaved('Mine', exampleBody())), 'Mine', 'overview', quiet);

    const tabs = screen.getByRole('navigation', { name: 'Options setup tabs' });

    expect(within(tabs).queryByRole('link', { name: /Explore/ })).toBeNull();
    expect(within(tabs).queryByRole('link', { name: /Golden/ })).toBeNull();
    expect(tabs.textContent).toContain('Explore');
    expect(tabs.textContent).toContain('a backtest cannot be started from here yet');
    expect(tabs.textContent).toContain('Approving a tested run and starting a golden search are being moved into this place');
  });

  it('opens the manual from the header in a new tab, so the half-built setup stays as it is', async () => {
    await renderTab(withLibrary(servedSaved('Mine', exampleBody())), 'Mine', 'overview', quiet);

    const link = await screen.findByRole('link', { name: 'How this works' });

    expect(link.getAttribute('href')).toBe('/docs/options-backtest-manual');
    expect(link.getAttribute('target')).toBe('_blank');
  });
});

describe('a draft with no saved name', () => {
  it('lives at its own address, is titled as new, and says it is not saved', async () => {
    await renderTab(withLibrary(), null, 'define', { ...quiet, form: false });

    expect(await screen.findByRole('heading', { level: 1, name: 'New options setup' })).toBeTruthy();
    expect(screen.getByText('Not saved')).toBeTruthy();
    expect(within(screen.getByRole('navigation', { name: 'Options setup tabs' })).getByRole('link', { name: 'Define' }).getAttribute('href')).toBe('/options-setups/_new/define');
  });
});

