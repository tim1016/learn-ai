import { TestBed } from '@angular/core/testing';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fleetPage, harnessEntry, proofRecord, qualification, registryEntry, runningBot } from './testing/fixtures';
import { emptyWorld, provideSignalStrategyWorld, type SignalStrategyReads, type SignalStrategyWorld } from './testing/signal-strategy-world';

afterEach(() => {
  TestBed.resetTestingModule();
});

async function openList(world: SignalStrategyWorld, reads: Partial<SignalStrategyReads> = {}) {
  TestBed.configureTestingModule({ providers: provideSignalStrategyWorld(world, reads) });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl('/signal-strategies');
  return harness;
}

/** The table row that names a strategy. */
async function rowOf(name: string): Promise<HTMLElement> {
  const row = (await screen.findByRole('link', { name })).closest('tr');
  if (row === null) throw new Error(`${name} is not in a row`);
  return row;
}

const SMA = registryEntry({
  strategy_key: 'sma_crossover',
  display_name: 'SMA Crossover',
  capabilities: {
    sweep: { supported: true, reason: null },
    golden: { supported: false, reason: 'No Golden Search declaration has shipped for this strategy yet.' },
    live: { supported: true, reason: null },
  },
});
const COMPATIBILITY = registryEntry({ strategy_key: 'compat_ema', display_name: 'Compat EMA', catalog_visible: false });

describe('the Signal strategies list', () => {
  it('lists every registry entry, the one the picker hides and the harness included, with what each allows and why not', async () => {
    await openList(emptyWorld({ entries: [registryEntry(), SMA, harnessEntry(), COMPATIBILITY] }));

    for (const name of ['EMA Crossover Signal', 'SMA Crossover', 'Deployment Validation', 'Compat EMA']) {
      expect(await screen.findByRole('link', { name })).toBeTruthy();
    }
    expect(within(await rowOf('Compat EMA')).getByText(/Compatibility entry, not offered in Strategy Lab's picker/)).toBeTruthy();

    const sma = within(await rowOf('SMA Crossover'));
    expect(sma.getByText('Sweep: allowed')).toBeTruthy();
    expect(sma.getByText('Golden Search: not allowed')).toBeTruthy();
    expect(sma.getByText('No Golden Search declaration has shipped for this strategy yet.')).toBeTruthy();

    const harness = within(await rowOf('Deployment Validation'));
    expect(harness.getByText('Experimental validation only — not a trading strategy')).toBeTruthy();
    expect(harness.getByText('Live: not allowed')).toBeTruthy();
    expect(harness.getByText(/stays outside Live/)).toBeTruthy();
  });

  it('opens a strategy’s Overview from its row, and offers the Recency comparison from the header', async () => {
    await openList(emptyWorld({ entries: [registryEntry()] }));

    expect((await screen.findByRole('link', { name: 'EMA Crossover Signal' })).getAttribute('href')).toBe(
      '/signal-strategies/ema_crossover_signal/overview',
    );
    expect(screen.getByRole('link', { name: 'Recency chart' }).getAttribute('href')).toBe('/signal-strategies/recency');
  });

  it('puts each fact in its own column: proof as the program’s, versions by symbol, and running bots by mode', async () => {
    await openList(
      emptyWorld({
        entries: [registryEntry(), SMA],
        proof: [proofRecord('ema_crossover_signal', 'stale')],
        qualifications: [
          qualification({ symbol: 'SPY', status: 'ready' }),
          qualification({ id: 'gq-2', symbol: 'QQQ', status: 'revoked' }),
        ],
        fleet: fleetPage({ rows: [runningBot({ world: 'dry_run', strategy_instance_id: 'dry' }), runningBot()] }),
      }),
    );

    const ema = within(await rowOf('EMA Crossover Signal'));
    expect(await ema.findByText('Stale')).toBeTruthy();
    expect(ema.getByText('1 ready')).toBeTruthy();
    expect(ema.getByText('1 revoked')).toBeTruthy();
    expect(ema.getByText(/2 running/)).toBeTruthy();
    expect(ema.getByText('Paper · 1 bot')).toBeTruthy();
    expect(ema.getByText('Dry Run · 1 bot')).toBeTruthy();

    const sma = within(await rowOf('SMA Crossover'));
    expect(sma.getByText('No proof record')).toBeTruthy();
    expect(sma.getByText('None')).toBeTruthy();
    expect(sma.getByText('None running')).toBeTruthy();
  });

  it('turns only the running column to unknown when the fleet read fails, and says why', async () => {
    await openList(emptyWorld({ entries: [registryEntry()], proof: [proofRecord('ema_crossover_signal')], fleet: new Error('fleet down') }));

    const row = within(await rowOf('EMA Crossover Signal'));

    expect(await screen.findByText('Running bots could not be read.')).toBeTruthy();
    expect(row.getByText('Unknown')).toBeTruthy();
    expect(row.queryByText('None running')).toBeNull();
    expect(row.getByText('Current')).toBeTruthy();
  });

  it('never calls a strategy idle when the fleet read could not see every account', async () => {
    const unreadable = { broker: 'alpaca', clerk_id: 'clrk_other', account_id: null, strategy_instance_id: null, reason: 'unreachable', reason_code: null };
    await openList(emptyWorld({ entries: [registryEntry()], fleet: fleetPage({ rows: [], gaps: [unreadable] }) }));

    const row = within(await rowOf('EMA Crossover Signal'));

    expect(await row.findByText('Unknown')).toBeTruthy();
    expect(row.queryByText('None running')).toBeNull();
    expect(screen.getByText(/could not see 1 account, so a strategy with none running there is unknown, not idle/)).toBeTruthy();
  });

  it('lists nothing as known when the registry cannot be read, and reads it again on request', async () => {
    const entries = vi.fn();
    await openList(emptyWorld({ entries: new Error('registry down') }), { entries });

    expect(await screen.findByText('The strategy registry could not be read.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Read it again' }));

    await waitFor(() => expect(entries).toHaveBeenCalledTimes(2));
  });
});
