import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { harnessEntry, qualification, registryEntry } from '../testing/fixtures';
import { emptyWorld, provideSignalStrategyWorld, type SignalStrategyReads, type SignalStrategyWorld } from '../testing/signal-strategy-world';

const EMA = 'ema_crossover_signal';

afterEach(() => {
  TestBed.resetTestingModule();
});

async function open(url: string, world: SignalStrategyWorld, reads: Partial<SignalStrategyReads> = {}) {
  TestBed.configureTestingModule({ providers: provideSignalStrategyWorld(world, reads) });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return harness;
}

async function openChooser(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole('button', { name: 'Deploy a bot' }));
  return screen.findByRole('region', { name: 'Deploy a bot' });
}

const SPY_DEFAULT = qualification({ id: 'spy-default', symbol: 'SPY', status: 'ready', is_default: true, created_at_ms: 4, parameters: { fast_period: 5, slow_period: 10 } });
const SPY_EARLIER = qualification({ id: 'spy-earlier', symbol: 'SPY', status: 'ready', created_at_ms: 3, parameters: { fast_period: 6, slow_period: 12 } });
const QQQ_READY = qualification({ id: 'qqq-ready', symbol: 'QQQ', status: 'ready', is_default: true, created_at_ms: 2, parameters: { fast_period: 8, slow_period: 21 } });

describe('Deploy a bot, from a strategy’s header', () => {
  it('is a header button on every tab that goes nowhere by itself', async () => {
    const harness = await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], qualifications: [SPY_DEFAULT] }));

    const button = await screen.findByRole('button', { name: 'Deploy a bot' });
    expect(screen.queryByRole('link', { name: 'Deploy a bot' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Deploy a bot' })).toBeNull();

    fireEvent.click(button);
    await screen.findByRole('region', { name: 'Deploy a bot' });
    expect(TestBed.inject(Router).url).toBe(`/signal-strategies/${EMA}/overview`);

    await harness.navigateByUrl(`/signal-strategies/${EMA}/define`);
    expect(await screen.findByRole('button', { name: 'Deploy a bot' })).toBeTruthy();
  });

  it('lists each symbol’s versions and links a ready one by its own id, the current default included', async () => {
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], qualifications: [SPY_EARLIER, SPY_DEFAULT, QQQ_READY] }));

    const chooser = await openChooser();

    // The default is marked where it is displayed, and taken by the id shown there.
    const takeDefault = within(chooser).getByRole('link', { name: 'Use the current default spy-default for SPY' });
    expect(takeDefault.getAttribute('href')).toBe(`/brokers/alpaca?strategy=${EMA}&golden_qualification=spy-default`);
    expect(within(chooser).getAllByText('default for SPY')).toHaveLength(1);
    // Another ready version of the same symbol is the owner's own pick, by its own id.
    const pickEarlier = within(chooser).getByRole('link', { name: 'Deploy version spy-earlier for SPY' });
    expect(pickEarlier.getAttribute('href')).toBe(`/brokers/alpaca?strategy=${EMA}&golden_qualification=spy-earlier`);
    expect(within(chooser).getByRole('link', { name: 'Use the current default qqq-ready for QQQ' }).getAttribute('href'))
      .toBe(`/brokers/alpaca?strategy=${EMA}&golden_qualification=qqq-ready`);
    // Each version shows the exact configuration it would carry.
    expect(within(chooser).getByText('fast_period = 6 · slow_period = 12')).toBeTruthy();
    expect(within(chooser).getByText('fast_period = 8 · slow_period = 21')).toBeTruthy();
  });

  it('chooses no version and no account when it opens, however many versions there are', async () => {
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], qualifications: [SPY_DEFAULT] }));

    await openChooser();

    // One ready version is still the owner's click, not a pre-chosen one.
    expect(TestBed.inject(Router).url).toBe(`/signal-strategies/${EMA}/overview`);
  });

  it('lists a stale, revoked or unverifiable version with its reason and no link, and offers no other in its place', async () => {
    const staleDefault = qualification({ id: 'spy-stale', symbol: 'SPY', status: 'stale', is_default: true, status_explanation: 'The program changed since approval.', created_at_ms: 5 });
    const revoked = qualification({ id: 'qqq-revoked', symbol: 'QQQ', status: 'revoked', status_explanation: 'Revoked by the owner.', created_at_ms: 3 });
    const unknown = qualification({ id: 'iwm-unknown', symbol: 'IWM', status: 'unverifiable', status_explanation: 'Its status could not be read.', created_at_ms: 2 });
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], qualifications: [staleDefault, SPY_EARLIER, revoked, unknown] }));

    const chooser = await openChooser();

    expect(within(chooser).getByText('Not offered: The program changed since approval.')).toBeTruthy();
    expect(within(chooser).getByText('Not offered: Revoked by the owner.')).toBeTruthy();
    expect(within(chooser).getByText('Not offered: Its status could not be read.')).toBeTruthy();
    // The only version links are the one ready version, by its own id; the stale default is not swapped for it.
    const links = within(chooser).getAllByRole('link').map((link) => link.getAttribute('href'));
    expect(links).toEqual([
      `/brokers/alpaca?strategy=${EMA}&golden_qualification=spy-earlier`,
      `/brokers/alpaca?deploy=&strategy=${EMA}`,
    ]);
    expect(within(chooser).queryByRole('link', { name: /spy-stale/ })).toBeNull();
  });

  it('offers the strategy alone for one the registry gives no Golden Search, so no saved version is required', async () => {
    await open('/signal-strategies/deployment_validation/overview', emptyWorld({ entries: [harnessEntry()] }));

    const chooser = await openChooser();

    expect(within(chooser).getByText('This strategy is an operational harness, not a production candidate.')).toBeTruthy();
    const alone = within(chooser).getByRole('link', { name: 'Choose an account for deployment_validation without a saved version' });
    expect(alone.getAttribute('href')).toBe('/brokers/alpaca?deploy=&strategy=deployment_validation');
    expect(within(chooser).getAllByRole('link')).toHaveLength(1);
  });

  it('still offers the strategy alone when its versions cannot be read, and reads them again on request', async () => {
    const qualifications = vi.fn();
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], qualifications: new Error('research database down') }), { qualifications });

    const chooser = await openChooser();

    expect(await within(chooser).findByText('This strategy’s qualified versions could not be read.')).toBeTruthy();
    // Unknown, never none: no version row stands in for the failed read.
    expect(within(chooser).queryByText(/No symbol has a qualified version/)).toBeNull();
    expect(within(chooser).getByRole('link', { name: `Choose an account for ${EMA} without a saved version` })).toBeTruthy();

    const before = qualifications.mock.calls.length;
    fireEvent.click(within(chooser).getByRole('button', { name: 'Read them again' }));
    await waitFor(() => expect(qualifications.mock.calls.length).toBeGreaterThan(before));
  });

  it('closes from its own button and from the header button, and stays closed for another strategy', async () => {
    const harness = await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry(), harnessEntry()], qualifications: [SPY_DEFAULT] }));

    const chooser = await openChooser();
    fireEvent.click(within(chooser).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Deploy a bot' })).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Deploy a bot' }));
    await screen.findByRole('region', { name: 'Deploy a bot' });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy a bot' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Deploy a bot' })).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Deploy a bot' }));
    await screen.findByRole('region', { name: 'Deploy a bot' });
    await harness.navigateByUrl('/signal-strategies/deployment_validation/overview');
    await screen.findByRole('heading', { level: 1, name: 'Deployment Validation' });
    expect(screen.queryByRole('region', { name: 'Deploy a bot' })).toBeNull();
  });
});
