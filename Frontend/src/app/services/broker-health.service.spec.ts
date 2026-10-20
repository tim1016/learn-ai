import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi, afterEach } from 'vitest';
import { BrokerHealthService } from './broker-health.service';
import { MarketDataFeedService } from './market-data-feed.service';
import type { IbkrConnectionHealth } from '../api/broker-models';

class FakeMarketDataFeedService {
  health = vi.fn();
}

function makeHealth(overrides: Partial<IbkrConnectionHealth> = {}): IbkrConnectionHealth {
  return {
    mode: 'paper',
    host: '127.0.0.1',
    port: 4002,
    client_id: 1,
    connected: true,
    account_id: 'DU1234567',
    is_paper: true,
    server_version: 178,
    fetched_at_ms: 1_700_000_000_000,
    connection_state: 'connected',
    last_transition_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function setup() {
  const broker = new FakeMarketDataFeedService();
  TestBed.configureTestingModule({
    providers: [{ provide: MarketDataFeedService, useValue: broker }],
  });
  const svc = TestBed.inject(BrokerHealthService);
  return { svc, broker };
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerHealthService — bannerState', () => {
  it('returns null when health has not yet loaded', () => {
    const { svc } = setup();
    expect(svc.bannerState()).toBeNull();
  });

  it.each([
    ['returns disabled when health is disabled (takes priority)', { disabled: true, connected: false }, 'disabled'],
    ['remains disabled even when broker reports connected', { disabled: true, connected: true, is_paper: true }, 'disabled'],
    ['returns disconnected when connected is false and not disabled', { connected: false, is_paper: null }, 'disconnected'],
    ['returns degraded when reconnect recovery exhausts even if connected is false',
      { connected: false, is_paper: null, connection_state: 'hard_down' }, 'degraded'],
    ['returns paper when connected and is_paper is true', { connected: true, is_paper: true }, 'paper'],
    ['returns live when connected and is_paper is false', { connected: true, is_paper: false }, 'live'],
    ['returns degraded when the socket is connected but broker state is not ready', {
      connected: true,
      is_paper: true,
      connection_state: 'subscriptions_stale',
    }, 'degraded'],
  ] as const)('derives the banner from loaded health: %s', (_name, overrides, expected) => {
    const { svc } = setup();
    svc.health.set(makeHealth(overrides));
    expect(svc.bannerState()).toBe(expected);
  });
});

describe('BrokerHealthService — refresh()', () => {
  it('populates health from broker.health() response', async () => {
    const { svc, broker } = setup();
    const snapshot = makeHealth();
    broker.health.mockResolvedValue(snapshot);

    await svc.refresh();

    expect(svc.health()).toEqual(snapshot);
    expect(svc.lastError()).toBeNull();
  });

  it('sets health to null and captures lastError when broker.health() rejects', async () => {
    const { svc, broker } = setup();
    const err = new Error('network down');
    broker.health.mockRejectedValue(err);

    await svc.refresh();

    expect(svc.health()).toBeNull();
    expect(svc.lastError()).toBe(err);
  });

  it('updates bannerState reactively after refresh', async () => {
    const { svc, broker } = setup();
    expect(svc.bannerState()).toBeNull();

    broker.health.mockResolvedValue(makeHealth({ disabled: true }));
    await svc.refresh();

    expect(svc.bannerState()).toBe('disabled');
  });
});
