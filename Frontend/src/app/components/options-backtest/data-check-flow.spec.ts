import { describe, expect, it, vi } from 'vitest';

import { DataCheckFlow } from './data-check-flow';
import { RunRefusedError } from './options-backtest.service';
import type { DataCheck } from './options-backtest.types';
import { dataCheck, exampleBody, PRICES_FROM_DAY, sessionCheck } from './testing/fixtures';
import { etMidnightMs } from '../../shared/date/et-midnight';

const RANGE = { start_ms: 1_000, end_ms: 2_000 };

/** A check whose answer a spec delivers when it chooses. */
function held() {
  const calls: { resolve: (answer: DataCheck) => void; reject: (error: unknown) => void }[] = [];
  const flow = new DataCheckFlow(
    () =>
      new Promise<DataCheck>((resolve, reject) => {
        calls.push({ resolve, reject });
      }),
  );
  return { flow, calls };
}

describe('asking Check data', () => {
  it('shows the answer with the setup and dates it was asked with, and keeps from when option prices are available', async () => {
    const flow = new DataCheckFlow(async () => dataCheck([sessionCheck('2026-09-14')]));

    const pending = flow.run(exampleBody(), RANGE, 'stamp-1');
    expect(flow.state().kind).toBe('checking');
    await pending;

    const state = flow.state();
    expect(state).toMatchObject({ kind: 'ready', asked: { start_ms: 1_000, end_ms: 2_000, stamp: 'stamp-1' } });
    expect(flow.pricesFromMs()).toBe(etMidnightMs(PRICES_FROM_DAY));
  });

  it('hands the check the setup and the two instants', async () => {
    const check = vi.fn(async () => dataCheck([]));
    const flow = new DataCheckFlow(check);

    await flow.run(exampleBody(), RANGE, 's');

    expect(check).toHaveBeenCalledWith(exampleBody(), 1_000, 2_000);
  });

  it('drops an older answer that arrives after a newer one was asked for', async () => {
    const { flow, calls } = held();

    void flow.run(exampleBody(), RANGE, 'old');
    void flow.run(exampleBody(), RANGE, 'new');
    calls[1]?.resolve(dataCheck([], { option_prices_from_ms: 222 }));
    await Promise.resolve();
    calls[0]?.resolve(dataCheck([], { option_prices_from_ms: 111 }));
    await Promise.resolve();
    await Promise.resolve();

    expect(flow.state()).toMatchObject({ kind: 'ready', asked: { stamp: 'new' } });
    expect(flow.pricesFromMs()).toBe(222);
  });

  it('closes on dismissal, drops an answer still on its way, and keeps the day prices are available from', async () => {
    const { flow, calls } = held();
    const first = flow.run(exampleBody(), RANGE, 'a');
    calls[0]?.resolve(dataCheck([], { option_prices_from_ms: 5 }));
    await first;

    void flow.run(exampleBody(), RANGE, 'b');
    flow.dismiss();
    calls[1]?.resolve(dataCheck([], { option_prices_from_ms: 6 }));
    await Promise.resolve();
    await Promise.resolve();

    expect(flow.state().kind).toBe('idle');
    expect(flow.pricesFromMs()).toBe(5);
  });
});

describe('when Check data does not answer', () => {
  it('keeps a range the server refuses apart, with its message, and shows no failed panel', async () => {
    const flow = new DataCheckFlow(async () => {
      throw new RunRefusedError('OPTIONS_BACKTEST_RANGE_TOO_LONG', 'Too many sessions.', 400);
    });

    await flow.run(exampleBody(), RANGE, 's');

    expect(flow.refusal()).toBe('Too many sessions.');
    expect(flow.state().kind).toBe('idle');
    flow.clearRefusal();
    expect(flow.refusal()).toBeNull();
  });

  it('says the server’s message when the catalog cannot be asked, as a failure of the check', async () => {
    const flow = new DataCheckFlow(async () => {
      throw new RunRefusedError('OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE', 'The data catalog cannot be asked right now.', 503);
    });

    await flow.run(exampleBody(), RANGE, 's');

    expect(flow.state()).toEqual({ kind: 'failed', message: 'The data catalog cannot be asked right now.' });
    expect(flow.refusal()).toBeNull();
  });

  it('says plainly when the check could not be made at all', async () => {
    const flow = new DataCheckFlow(async () => {
      throw new Error('offline');
    });

    await flow.run(exampleBody(), RANGE, 's');

    expect(flow.state()).toEqual({ kind: 'failed', message: 'The data could not be checked. Try again.' });
  });

  it('forgets an earlier refusal when it is asked again', async () => {
    let refuse = true;
    const flow = new DataCheckFlow(async () => {
      if (refuse) throw new RunRefusedError('X', 'No.', 400);
      return dataCheck([]);
    });
    await flow.run(exampleBody(), RANGE, 's');

    refuse = false;
    await flow.run(exampleBody(), RANGE, 's');

    expect(flow.refusal()).toBeNull();
    expect(flow.state().kind).toBe('ready');
  });
});
