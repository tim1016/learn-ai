import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { chainStreamUrl, LiveChainFeed } from './live-chain-feed.service';
import { FAR, NEAR, fakeIbkrFeed, ibkrFrame } from './testing/fake-ibkr';

function feedOf(world = fakeIbkrFeed()) {
  TestBed.configureTestingModule({ providers: [LiveChainFeed, world.provide] });
  return { feed: TestBed.inject(LiveChainFeed), ...world };
}

const chain = { symbol: 'SPY', expiryMs: NEAR, strikes: [501, 499, 500] };

describe('the page\'s one live subscription', () => {
  afterEach(() => vi.useRealTimers());

  it('opens one stream for the strikes asked, in order, and holds nothing before that', () => {
    const { feed, streams } = feedOf();
    expect(feed.state()).toBe('none');
    expect(streams).toHaveLength(0);

    feed.start({ ...chain, symbol: 'spy' });

    expect(streams).toHaveLength(1);
    expect(streams[0].url).toBe(chainStreamUrl('SPY', NEAR, [499, 500, 501]));
    expect(streams[0].url).toBe(`/api/broker/option-chain/SPY?expiry_ms=${NEAR}&strikes=499&strikes=500&strikes=501&debounce_ms=500`);
    expect(feed.subscription()).toEqual({ symbol: 'SPY', expiryMs: NEAR, strikes: [499, 500, 501] });
  });

  it('shows the latest frame of the subscription it holds', () => {
    const { feed, streams } = feedOf();
    feed.start(chain);
    expect(feed.snapshot()).toBeNull();

    streams[0].emit(ibkrFrame());

    expect(feed.state()).toBe('open');
    expect(feed.snapshot()).toEqual(ibkrFrame());
  });

  it('releases the old stream before it opens another, and a late frame from the old one is never shown', () => {
    const { feed, streams } = feedOf();
    feed.start(chain);
    streams[0].emit(ibkrFrame({ underlying_price: 111 }));

    feed.start({ ...chain, expiryMs: FAR });
    streams[0].emit(ibkrFrame({ underlying_price: 222 }));

    expect(streams).toHaveLength(2);
    expect(streams[0].closes).toBe(1);
    expect(feed.snapshot()).toBeNull();
    streams[1].emit(ibkrFrame({ expiry_ms: FAR, underlying_price: 333 }));
    expect(feed.snapshot()?.underlying_price).toBe(333);
  });

  it('shows nothing of a frame that names another underlying or expiry than the one subscribed', () => {
    const { feed, streams } = feedOf();
    feed.start(chain);

    streams[0].emit(ibkrFrame({ expiry_ms: FAR }));
    expect(feed.snapshot()).toBeNull();
    streams[0].emit(ibkrFrame({ symbol: 'QQQ' }));
    expect(feed.snapshot()).toBeNull();
    streams[0].emit(ibkrFrame());
    expect(feed.snapshot()).not.toBeNull();
  });

  it('lets go of everything on release: the stream is closed and no frame of it is kept or shown', () => {
    const { feed, streams } = feedOf();
    feed.start(chain);
    streams[0].emit(ibkrFrame());

    feed.release();
    streams[0].emit(ibkrFrame({ underlying_price: 999 }));

    expect(streams[0].closes).toBe(1);
    expect(feed.state()).toBe('none');
    expect(feed.subscription()).toBeNull();
    expect(feed.snapshot()).toBeNull();
  });

  it('releases the stream when the page is left', () => {
    const { feed, streams } = feedOf();
    feed.start(chain);

    TestBed.resetTestingModule();

    expect(streams[0].closes).toBe(1);
    expect(feed.state()).toBe('none');
  });

  it('pauses by closing the stream and keeping the last snapshot to show as stale, and resumes with a new stream for the same chain', () => {
    const { feed, streams } = feedOf();
    feed.start(chain);
    streams[0].emit(ibkrFrame({ underlying_price: 111 }));

    feed.pause();
    streams[0].emit(ibkrFrame({ underlying_price: 222 }));

    expect(streams[0].closes).toBe(1);
    expect(feed.state()).toBe('stopped');
    expect(feed.snapshot()?.underlying_price).toBe(111);
    expect(feed.live()).toBe(false);

    feed.resume();

    expect(streams).toHaveLength(2);
    expect(streams[1].url).toBe(streams[0].url);
    expect(feed.snapshot()).toBeNull();
  });

  it('reports a stream that stopped with the reason it gave, and does not call it live', () => {
    const { feed, streams } = feedOf();
    feed.start(chain);
    streams[0].emit(ibkrFrame());
    expect(feed.live()).toBe(true);

    streams[0].fail('The option-chain stream stopped: the broker dropped the market-data request.');

    expect(feed.state()).toBe('error');
    expect(feed.error()).toBe('The option-chain stream stopped: the broker dropped the market-data request.');
    expect(feed.live()).toBe(false);
  });

  it('stops calling the snapshot current once no frame has arrived for the stall limit, though the stream never reported an error', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    const { feed, streams } = feedOf();
    feed.start(chain);
    streams[0].emit(ibkrFrame());
    expect(feed.live()).toBe(true);

    await vi.advanceTimersByTimeAsync(4_000);
    expect(feed.live()).toBe(true);
    await vi.advanceTimersByTimeAsync(2_000);

    expect(feed.state()).toBe('open');
    expect(feed.live()).toBe(false);
    streams[0].emit(ibkrFrame());
    await vi.advanceTimersByTimeAsync(1_000);
    expect(feed.live()).toBe(true);
  });
});
