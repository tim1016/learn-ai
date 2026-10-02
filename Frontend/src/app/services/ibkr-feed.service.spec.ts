import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import type { ClerkStatus } from '../api/alpaca.types';
import { provideFleetDirectory, testLane } from '../fleet/fleet-directory-testing';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import { BrokersService } from './brokers.service';
import { IbkrFeedService, laneFeedState } from './ibkr-feed.service';

function status(overrides: Partial<ClerkStatus> = {}): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: 'PA3KWXU1C4C3',
    hold: { active: false },
    outstanding_intents: 0,
    observed_at_ms: 1_790_948_794_801,
    channel_healths: [
      { stream: 'market_data', healthy: true, connected: true, reason: '', observed_at_ms: 1_790_948_794_801 },
      { stream: 'execution', healthy: true, connected: true, reason: '', observed_at_ms: 1_790_948_794_801 },
    ],
    ...overrides,
  };
}

const LOST = { stream: 'market_data', healthy: false, connected: false, reason: 'IBKR connection lost', observed_at_ms: 1 } as const;

describe('laneFeedState', () => {
  it('reads a symbol still warming up as connected — logging in to IB Gateway cannot fix warm-up', () => {
    const warming = { ...LOST, connected: true, reason: 'Active IBKR feed for SPY has not produced its first closed bar' };

    expect(laneFeedState(status({ channel_healths: [warming] }))).toEqual({ kind: 'connected' });
  });

  it('dates a lost connection from the stream-health hold it raised', () => {
    const held = status({
      channel_healths: [LOST],
      hold: { active: true, reason_code: 'STREAM_HEALTH_HOLD', since_ms: 1_790_916_315_993 },
    });

    expect(laneFeedState(held)).toEqual({ kind: 'disconnected', reason: 'IBKR connection lost', sinceMs: 1_790_916_315_993 });
  });

  it('does not borrow the start of a hold with another cause', () => {
    const held = status({
      channel_healths: [LOST],
      hold: { active: true, reason_code: 'LIVE_ENVELOPE_LOSS_HOLD', since_ms: 1_790_000_000_000 },
    });

    expect(laneFeedState(held)).toEqual({ kind: 'disconnected', reason: 'IBKR connection lost', sinceMs: null });
  });

  it('is unknown when the Clerk reports no market-data channel at all', () => {
    expect(laneFeedState(status({ channel_healths: null }))).toEqual({ kind: 'unknown' });
  });
});

describe('IbkrFeedService', () => {
  it('waits for the roster on a cold boot instead of reading no lanes', async () => {
    let loaded = false;
    TestBed.configureTestingModule({
      providers: [
        {
          provide: FleetDirectoryService,
          useValue: {
            ensureLoaded: async () => { loaded = true; },
            lanesOf: () => (loaded ? [testLane({ clerk_id: 'clrk_paper' })] : []),
          },
        },
        { provide: BrokersService, useValue: { getClerkStatuses: () => [Promise.resolve(status({ channel_healths: [LOST] }))] } },
      ],
    });
    const service = TestBed.inject(IbkrFeedService);

    await service.refresh();

    expect(service.stateByClerkId().get('clrk_paper')?.kind).toBe('disconnected');
  });

  it('publishes a fast lane’s outage while a slow lane is still reading', async () => {
    TestBed.configureTestingModule({
      providers: [
        provideFleetDirectory({
          observed_at_ms: 1_790_000_000_000,
          clerks: [testLane({ clerk_id: 'clrk_paper' }), testLane({ clerk_id: 'clrk_live' })],
        }),
        {
          provide: BrokersService,
          useValue: {
            getClerkStatuses: () => [
              Promise.resolve(status({ channel_healths: [LOST] })),
              new Promise<ClerkStatus>(() => undefined),
            ],
          },
        },
      ],
    });
    const service = TestBed.inject(IbkrFeedService);

    void service.refresh();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(service.stateByClerkId().get('clrk_paper')?.kind).toBe('disconnected');
    expect(service.stateByClerkId().has('clrk_live')).toBe(false);
  });

  it('keeps each lane’s own answer, and a failed read is unknown, never connected', async () => {
    TestBed.configureTestingModule({
      providers: [
        provideFleetDirectory({
          observed_at_ms: 1_790_000_000_000,
          clerks: [testLane({ clerk_id: 'clrk_paper' }), testLane({ clerk_id: 'clrk_live' })],
        }),
        {
          provide: BrokersService,
          useValue: {
            getClerkStatuses: () => [
              Promise.resolve(status({ channel_healths: [LOST] })),
              Promise.reject(new Error('lane unreachable')),
            ],
          },
        },
      ],
    });
    const service = TestBed.inject(IbkrFeedService);

    await service.refresh();

    expect(service.stateByClerkId().get('clrk_paper')).toEqual({ kind: 'disconnected', reason: 'IBKR connection lost', sinceMs: null });
    expect(service.stateByClerkId().get('clrk_live')).toEqual({ kind: 'unknown' });
  });
});
