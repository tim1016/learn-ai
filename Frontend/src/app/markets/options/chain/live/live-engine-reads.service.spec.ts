import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { IbkrChainSnapshot } from '../../../../api/broker-models';
import type { QuantLibPriceResult } from '../../../../services/market-data.types';
import { QuantLibService } from '../../../../services/quantlib.service';
import { deferred } from '../../../../components/options-backtest/testing/deferred';
import { contractKey, type CrossCheckContext } from './live-cross-check';
import { LiveEngineReads } from './live-engine-reads.service';
import { AS_OF_MS, FAR, ibkrFrame, ibkrQuote, engineReply, silentQuote } from './testing/fake-ibkr';

/** The chain with one call at 500, valued with the given spot and volatility. */
function chain(spot = 500, iv = 0.2, changes: Partial<IbkrChainSnapshot> = {}): IbkrChainSnapshot {
  return ibkrFrame({ underlying_price: spot, quotes: [ibkrQuote(500, 'C', { iv })], ...changes });
}

function reads(priceOption: ReturnType<typeof vi.fn>) {
  TestBed.configureTestingModule({
    providers: [LiveEngineReads, { provide: QuantLibService, useValue: { priceOption } }],
  });
  const service = TestBed.inject(LiveEngineReads);
  const offer = (snapshot: IbkrChainSnapshot, live = true) => service.offer(snapshot, { live, snapshot, engine: 'analytic_bs' } satisfies CrossCheckContext);
  const answerFor = (strike = 500) => service.answers().get(contractKey(strike, 'C'));
  const deltaFor = (strike = 500) => {
    const answer = answerFor(strike);
    return answer?.kind === 'done' ? answer.delta : null;
  };
  return { service, offer, answerFor, deltaFor };
}

const settled = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe('the engine\'s side of the live cross-check', () => {
  it('asks again when the volatility, the spot or the expiry moves, though the bid/ask midpoint did not', async () => {
    const priceOption = vi.fn()
      .mockResolvedValueOnce(engineReply(0.5))
      .mockResolvedValueOnce(engineReply(0.6))
      .mockResolvedValueOnce(engineReply(0.7))
      .mockResolvedValueOnce(engineReply(0.8));
    const { offer, deltaFor } = reads(priceOption);

    offer(chain());
    await vi.waitFor(() => expect(deltaFor()).toBe(0.5));
    offer(chain());
    expect(priceOption).toHaveBeenCalledTimes(1);

    offer(chain(500, 0.25));
    await vi.waitFor(() => expect(deltaFor()).toBe(0.6));
    offer(chain(501, 0.25));
    await vi.waitFor(() => expect(deltaFor()).toBe(0.7));
    offer(chain(501, 0.25, { expiry_ms: FAR }));
    await vi.waitFor(() => expect(deltaFor()).toBe(0.8));
    expect(priceOption).toHaveBeenCalledTimes(4);
  });

  it('values the engine with the contract\'s IBKR volatility and the snapshot\'s underlying price, for its own strike, right and expiry', async () => {
    const priceOption = vi.fn().mockResolvedValue(engineReply(0.5));
    const { offer } = reads(priceOption);

    offer(ibkrFrame({ underlying_price: 503.25, quotes: [ibkrQuote(505, 'P', { iv: 0.31 })] }));

    expect(priceOption).toHaveBeenCalledWith({ spot: 503.25, strike: 505, volatility: 0.31, expirationMs: ibkrFrame().expiry_ms, optionType: 'put', engine: 'analytic_bs' });
  });

  it('holds no delta for inputs it was not priced with, keeps the failure of a read with its reason, and asks that input again', async () => {
    const priceOption = vi.fn()
      .mockResolvedValueOnce(engineReply(0.5))
      .mockResolvedValueOnce({ success: false, error: 'No engine result' } as QuantLibPriceResult)
      .mockResolvedValueOnce(engineReply(0.6))
      .mockRejectedValueOnce(new Error('Engine unavailable'));
    const { offer, answerFor, deltaFor } = reads(priceOption);

    offer(chain());
    await vi.waitFor(() => expect(deltaFor()).toBe(0.5));
    offer(chain(500, 0.25));
    await vi.waitFor(() => expect(answerFor()).toMatchObject({ kind: 'failed', why: 'No engine result' }));
    expect(deltaFor()).toBeNull();

    offer(chain(500, 0.25));
    await vi.waitFor(() => expect(deltaFor()).toBe(0.6));
    offer(chain(501, 0.25));
    await vi.waitFor(() => expect(answerFor()).toMatchObject({ kind: 'failed', why: 'Engine unavailable' }));
    expect(deltaFor()).toBeNull();
    expect(priceOption).toHaveBeenCalledTimes(4);
  });

  it('does not let an older answer overwrite the answer to the question now asked, at the same midpoint', async () => {
    const old = deferred<QuantLibPriceResult>();
    const priceOption = vi.fn().mockImplementationOnce(() => old.promise).mockResolvedValueOnce(engineReply(0.7));
    const { offer, deltaFor } = reads(priceOption);

    offer(chain());
    offer(chain(500, 0.25));
    expect(deltaFor()).toBeNull();
    await vi.waitFor(() => expect(deltaFor()).toBe(0.7));
    old.resolve(engineReply(0.5));
    await settled();

    expect(deltaFor()).toBe(0.7);
  });

  it('keeps one contract\'s failure on screen when another contract of the same batch answers later', async () => {
    const later = deferred<QuantLibPriceResult>();
    const priceOption = vi.fn()
      .mockResolvedValueOnce({ success: false, error: 'Strike 500 failed' } as QuantLibPriceResult)
      .mockImplementationOnce(() => later.promise);
    const { offer, answerFor, deltaFor } = reads(priceOption);

    offer(ibkrFrame({ quotes: [ibkrQuote(500, 'C'), ibkrQuote(501, 'C')] }));
    await vi.waitFor(() => expect(answerFor(500)).toMatchObject({ kind: 'failed', why: 'Strike 500 failed' }));
    later.resolve(engineReply(0.7));
    await vi.waitFor(() => expect(deltaFor(501)).toBe(0.7));

    expect(answerFor(500)).toMatchObject({ kind: 'failed' });
  });

  it('asks nothing of a contract that cannot be compared, and drops what it answered before it could not', async () => {
    const priceOption = vi.fn().mockResolvedValue(engineReply(0.5));
    const { offer, answerFor, deltaFor } = reads(priceOption);

    offer(ibkrFrame({ quotes: [silentQuote(500, 'C')] }));
    expect(priceOption).not.toHaveBeenCalled();

    offer(chain());
    await vi.waitFor(() => expect(deltaFor()).toBe(0.5));
    offer(ibkrFrame({ quotes: [ibkrQuote(500, 'C', { ts_ms: AS_OF_MS - 60_000 })] }));

    expect(answerFor()).toBeUndefined();
    expect(priceOption).toHaveBeenCalledTimes(1);
  });

  it('asks nothing, and holds no answer, while the stream is not delivering', async () => {
    const priceOption = vi.fn().mockResolvedValue(engineReply(0.5));
    const { offer, answerFor, deltaFor } = reads(priceOption);
    offer(chain());
    await vi.waitFor(() => expect(deltaFor()).toBe(0.5));

    offer(chain(), false);

    expect(answerFor()).toBeUndefined();
    expect(priceOption).toHaveBeenCalledTimes(1);
  });

  it('forgets every answer on a new chain, and drops an answer still in flight for the last one', async () => {
    const lastChain = deferred<QuantLibPriceResult>();
    const priceOption = vi.fn().mockImplementationOnce(() => lastChain.promise);
    const { service, offer, answerFor } = reads(priceOption);
    offer(chain());

    service.reset();
    lastChain.resolve(engineReply(0.9));
    await settled();

    expect(answerFor()).toBeUndefined();
    expect(service.answers().size).toBe(0);
  });
});
