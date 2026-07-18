import { provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { IbkrConnectionHealth, IbkrPnLTick } from '../../../api/broker-models';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import {
  makeAccountSummary,
  makeAccountTruth,
  makeBrokerHealth,
  makePosition,
  makePositionsSnapshot,
  makeTruthPosition,
} from './account-desk-holdings.fixtures';
import { AccountDeskHoldingsStore } from './account-desk-holdings-store.service';

class StubEventSource {
  static instances: StubEventSource[] = [];
  readonly listeners = new Map<string, ((event: Event) => void)[]>();
  readonly url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    StubEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: (event: Event) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }

  dispatch(name: string, data = ''): void {
    const event = new MessageEvent(name, { data });
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }

  close(): void {
    this.closed = true;
  }
}

describe('AccountDeskHoldingsStore', () => {
  const broker = {
    account: vi.fn(),
    positions: vi.fn(),
    accountTruth: vi.fn(),
  };
  let health: { health: ReturnType<typeof signal<IbkrConnectionHealth | null>> };

  beforeEach(() => {
    StubEventSource.instances = [];
    vi.stubGlobal('EventSource', StubEventSource);
    broker.account.mockReset();
    broker.positions.mockReset();
    broker.accountTruth.mockReset();
    health = { health: signal<IbkrConnectionHealth | null>(makeBrokerHealth()) };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskHoldingsStore,
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useValue: health },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.unstubAllGlobals();
  });

  it('attests the route before creating P&L subscriptions and folds only matching live ticks', async () => {
    const position = makePosition();
    broker.account.mockResolvedValue(makeAccountSummary());
    broker.positions.mockResolvedValue(makePositionsSnapshot(undefined, [position]));
    broker.accountTruth.mockResolvedValue(makeAccountTruth(undefined, [makeTruthPosition(position)]));
    const store = TestBed.inject(AccountDeskHoldingsStore);

    await store.load('DU1234567');

    expect(StubEventSource.instances).toHaveLength(2);
    expect(StubEventSource.instances[1].url).toContain('con_ids=12345');
    StubEventSource.instances[0].dispatch('pnl', JSON.stringify(accountTick()));
    StubEventSource.instances[1].dispatch('pnl', JSON.stringify(positionTick()));
    await settleEffects();

    expect(store.headlineMetrics()?.dayPnl).toBe(99);
    expect(store.rows()[0]?.pnl?.market_value).toBe(1_111);
    expect(store.rows()[0]?.owner.owner_label).toBe('Bot alpha');
  });

  it('rejects a mismatched session before positions, Account Truth, or streams are requested', async () => {
    broker.account.mockResolvedValue(makeAccountSummary('DU9999999'));
    const store = TestBed.inject(AccountDeskHoldingsStore);

    await store.load('DU1234567');

    expect(broker.positions).not.toHaveBeenCalled();
    expect(broker.accountTruth).not.toHaveBeenCalled();
    expect(StubEventSource.instances).toHaveLength(0);
    expect(store.rows()).toEqual([]);
    expect(store.unavailableMessage()).toContain('different account');
  });

  it('clears holdings and closes streams when a malformed P&L event arrives', async () => {
    const position = makePosition();
    broker.account.mockResolvedValue(makeAccountSummary());
    broker.positions.mockResolvedValue(makePositionsSnapshot(undefined, [position]));
    broker.accountTruth.mockResolvedValue(makeAccountTruth(undefined, [makeTruthPosition(position)]));
    const store = TestBed.inject(AccountDeskHoldingsStore);
    await store.load('DU1234567');

    StubEventSource.instances[0].dispatch('pnl', '{not-json');
    await settleEffects();

    expect(store.rows()).toEqual([]);
    expect(store.unavailableMessage()).toContain('malformed');
    expect(StubEventSource.instances.every((source) => source.closed)).toBe(true);
  });

  it('clears holdings when a P&L event belongs to a different account', async () => {
    const position = makePosition();
    broker.account.mockResolvedValue(makeAccountSummary());
    broker.positions.mockResolvedValue(makePositionsSnapshot(undefined, [position]));
    broker.accountTruth.mockResolvedValue(makeAccountTruth(undefined, [makeTruthPosition(position)]));
    const store = TestBed.inject(AccountDeskHoldingsStore);
    await store.load('DU1234567');

    StubEventSource.instances[1].dispatch(
      'pnl',
      JSON.stringify({ ...positionTick(), account_id: 'DU9999999' }),
    );
    await settleEffects();

    expect(store.rows()).toEqual([]);
    expect(store.unavailableMessage()).toContain('could not attest');
  });

  it('keeps last-good data visible when the P&L transport disconnects', async () => {
    const position = makePosition();
    broker.account.mockResolvedValue(makeAccountSummary());
    broker.positions.mockResolvedValue(makePositionsSnapshot(undefined, [position]));
    broker.accountTruth.mockResolvedValue(makeAccountTruth(undefined, [makeTruthPosition(position)]));
    const store = TestBed.inject(AccountDeskHoldingsStore);
    await store.load('DU1234567');

    StubEventSource.instances[0].dispatch('error');
    await settleEffects();

    expect(store.rows()).toHaveLength(1);
    expect(store.unavailableMessage()).toContain('disconnected');
    expect(store.showingStaleLastGood()).toBe(true);
  });

  it('keeps same-account last-good holdings when a refresh fails', async () => {
    const position = makePosition();
    broker.account.mockResolvedValueOnce(makeAccountSummary()).mockRejectedValueOnce(new Error('offline'));
    broker.positions.mockResolvedValue(makePositionsSnapshot(undefined, [position]));
    broker.accountTruth.mockResolvedValue(makeAccountTruth(undefined, [makeTruthPosition(position)]));
    const store = TestBed.inject(AccountDeskHoldingsStore);
    await store.load('DU1234567');

    await store.load('DU1234567');

    expect(store.rows()).toHaveLength(1);
    expect(store.showingStaleLastGood()).toBe(true);
    expect(store.error()).toBeInstanceOf(Error);
  });

  it('closes subscriptions and clears data on an account-route change', async () => {
    const first = makePosition('DU1234567');
    const second = makePosition('DU7654321', 76543);
    broker.account
      .mockResolvedValueOnce(makeAccountSummary('DU1234567'))
      .mockResolvedValueOnce(makeAccountSummary('DU7654321'));
    broker.positions
      .mockResolvedValueOnce(makePositionsSnapshot('DU1234567', [first]))
      .mockResolvedValueOnce(makePositionsSnapshot('DU7654321', [second]));
    broker.accountTruth
      .mockResolvedValueOnce(makeAccountTruth('DU1234567', [makeTruthPosition(first)]))
      .mockResolvedValueOnce(makeAccountTruth('DU7654321', [makeTruthPosition(second)]));
    const store = TestBed.inject(AccountDeskHoldingsStore);
    await store.load('DU1234567');
    const firstSources = [...StubEventSource.instances];

    await store.load('DU7654321');

    expect(firstSources.every((source) => source.closed)).toBe(true);
    expect(store.rows()[0]?.position.account_id).toBe('DU7654321');
  });

  it('invalidates holdings on shared broker changes and reloads only when the selected account is re-attested', async () => {
    const position = makePosition();
    broker.account.mockResolvedValue(makeAccountSummary());
    broker.positions.mockResolvedValue(makePositionsSnapshot(undefined, [position]));
    broker.accountTruth.mockResolvedValue(makeAccountTruth(undefined, [makeTruthPosition(position)]));
    const store = TestBed.inject(AccountDeskHoldingsStore);
    await store.load('DU1234567');
    const firstSources = [...StubEventSource.instances];

    health.health.set(makeBrokerHealth('DU1234567', { connected: false, connection_state: 'disconnected' }));
    await settleEffects();

    expect(firstSources.every((source) => source.closed)).toBe(true);
    expect(store.rows()).toEqual([]);
    expect(store.unavailableMessage()).toContain('disconnected');

    health.health.set(makeBrokerHealth('DU7654321'));
    await settleEffects();

    expect(broker.account).toHaveBeenCalledOnce();
    expect(store.rows()).toEqual([]);
    expect(store.unavailableMessage()).toContain('different account');

    health.health.set(makeBrokerHealth());
    await vi.waitFor(() => expect(broker.account).toHaveBeenCalledTimes(2));

    expect(store.rows()).toHaveLength(1);
    expect(store.rows()[0]?.position.account_id).toBe('DU1234567');
  });
});

function accountTick(): IbkrPnLTick {
  return {
    account_id: 'DU1234567',
    con_id: null,
    daily_pnl: 99,
    unrealized_pnl: 20,
    realized_pnl: 79,
    market_value: null,
    position: null,
    ts_ms: 1_780_000_003_000,
  };
}

function positionTick(): IbkrPnLTick {
  return {
    account_id: 'DU1234567',
    con_id: 12345,
    daily_pnl: 9,
    unrealized_pnl: 11,
    realized_pnl: 0,
    market_value: 1_111,
    position: 2,
    ts_ms: 1_780_000_003_000,
  };
}

async function settleEffects(): Promise<void> {
  await Promise.resolve();
  TestBed.tick();
  await Promise.resolve();
  TestBed.tick();
}
