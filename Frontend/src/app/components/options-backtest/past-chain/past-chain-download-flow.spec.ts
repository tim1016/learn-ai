import { describe, expect, it, vi } from 'vitest';

import { ChainDownloadFlow, type DownloadAsked } from './past-chain-download-flow';
import { DownloadNotAllowedError } from './past-chain.service';
import type { DownloadAnswer } from './past-chain.types';
import { downloadAnswer, item, TRADING_DATE_MS } from './testing/fixtures';

const ASKED: DownloadAsked = { underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, request: { underlying: 'SPY', trading_date_ms: TRADING_DATE_MS, contracts: ['SPY261016C00602000'] } };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe('a download being asked for', () => {
  it('is busy from the ask until the server answers, and then holds the answer with what it was asked for', async () => {
    const pending = deferred<DownloadAnswer>();
    const flow = new ChainDownloadFlow(() => pending.promise);

    const done = flow.run(ASKED);

    expect(flow.busy()).toBe(true);
    expect(flow.state()).toEqual({ kind: 'running', asked: ASKED });
    const answer = downloadAnswer([item('contract', 'SPY261016C00602000', 'fetched')]);
    pending.resolve(answer);
    await expect(done).resolves.toBe(true);
    expect(flow.busy()).toBe(false);
    expect(flow.state()).toEqual({ kind: 'answered', asked: ASKED, answer });
  });

  it('is asked once however many times it is pressed while it runs', async () => {
    const pending = deferred<DownloadAnswer>();
    const send = vi.fn(() => pending.promise);
    const flow = new ChainDownloadFlow(send);

    const first = flow.run(ASKED);
    const second = flow.run(ASKED);
    const third = flow.run(ASKED);

    await expect(second).resolves.toBe(false);
    await expect(third).resolves.toBe(false);
    pending.resolve(downloadAnswer([]));
    await first;
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('can be asked again once it has answered', async () => {
    const send = vi.fn(async () => downloadAnswer([]));
    const flow = new ChainDownloadFlow(send);

    await flow.run(ASKED);
    await flow.run(ASKED);

    expect(send).toHaveBeenCalledTimes(2);
  });

  it('says why when the page is not allowed, and keeps what it asked', async () => {
    const flow = new ChainDownloadFlow(async () => {
      throw new DownloadNotAllowedError('refused', 403);
    });

    await expect(flow.run(ASKED)).resolves.toBe(false);

    expect(flow.state()).toEqual({ kind: 'failed', asked: ASKED, problem: { kind: 'not_allowed', serverMessage: 'refused' } });
    expect(flow.busy()).toBe(false);
  });

  it('can be asked again after a failure', async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(downloadAnswer([]));
    const flow = new ChainDownloadFlow(send);

    await flow.run(ASKED);
    await expect(flow.run(ASKED)).resolves.toBe(true);

    expect(flow.state().kind).toBe('answered');
  });

  it('puts away an answer that no longer describes what is on screen', async () => {
    const flow = new ChainDownloadFlow(async () => downloadAnswer([]));
    await flow.run(ASKED);

    flow.clear();

    expect(flow.state()).toEqual({ kind: 'idle' });
  });

  it('leaves a download that is running alone: changing the date does not abandon it', async () => {
    const pending = deferred<DownloadAnswer>();
    const flow = new ChainDownloadFlow(() => pending.promise);
    const done = flow.run(ASKED);

    flow.clear();

    expect(flow.state()).toEqual({ kind: 'running', asked: ASKED });
    pending.resolve(downloadAnswer([]));
    await done;
    expect(flow.state().kind).toBe('answered');
  });
});
