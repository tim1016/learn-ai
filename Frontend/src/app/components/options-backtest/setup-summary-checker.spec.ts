import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SetupInvalidError } from './options-backtest.service';
import type { SetupRequest, SetupSummary } from './options-backtest.types';
import { SetupSummaryChecker } from './setup-summary-checker';
import { legKey } from './setup-problems';
import { exampleBody, summaryFor } from './testing/fixtures';

interface Call {
  readonly body: unknown;
  readonly resolve: (summary: SetupSummary) => void;
  readonly reject: (error: unknown) => void;
}

function fakeDescribe() {
  const calls: Call[] = [];
  const describe = vi.fn(
    (body: unknown) =>
      new Promise<SetupSummary>((resolve, reject) => {
        calls.push({ body, resolve, reject });
      }),
  );
  return { calls, describe };
}

const withCash = (cash: string): SetupRequest => ({ ...exampleBody(), starting_cash_usd: cash });
const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
};

describe('SetupSummaryChecker', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('waits for a pause in the editing, then asks once about the last setup', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 300);

    checker.show(withCash('1'));
    checker.show(withCash('12'));
    checker.show(withCash('123'));
    expect(checker.state().kind).toBe('checking');
    expect(describe).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(300);

    expect(describe).toHaveBeenCalledTimes(1);
    expect(calls[0]?.body).toEqual(withCash('123'));
  });

  it('shows the sentences the server gave', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(exampleBody());
    calls[0]?.resolve(summaryFor('first'));
    await flush();

    expect(checker.state()).toEqual({ kind: 'ready', summary: summaryFor('first'), body: exampleBody() });
  });

  it('never lets a slow answer to an earlier setup overwrite a newer one', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(withCash('1'));
    checker.show(withCash('2'));
    calls[1]?.resolve(summaryFor('newer'));
    await flush();
    calls[0]?.resolve(summaryFor('older'));
    await flush();

    expect(checker.state()).toEqual({ kind: 'ready', summary: summaryFor('newer'), body: withCash('2') });
  });

  it('ignores a late failure of an earlier setup too', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(withCash('1'));
    checker.show(withCash('2'));
    calls[1]?.resolve(summaryFor('newer'));
    await flush();
    calls[0]?.reject(new Error('timed out'));
    await flush();

    expect(checker.state().kind).toBe('ready');
  });

  it('drops an answer that arrives after the setup stopped being complete', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(exampleBody());
    checker.show(null);
    calls[0]?.resolve(summaryFor('late'));
    await flush();

    expect(checker.state()).toEqual({ kind: 'idle' });
  });

  it('puts a 422 at the legs it names, by id', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(exampleBody());
    calls[0]?.reject(new SetupInvalidError([{ loc: ['body', 'legs', 1, 'contracts'], msg: 'too many' }]));
    await flush();

    const state = checker.state();
    expect(state.kind).toBe('invalid');
    expect(state.kind === 'invalid' && state.problems.fields.get(legKey('put', 'contracts'))).toBe('too many');
  });

  it('says so when the server cannot be asked, and asks again on retry', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(exampleBody());
    calls[0]?.reject(new Error('network down'));
    await flush();
    expect(checker.state().kind).toBe('failed');

    checker.retry();
    expect(checker.state().kind).toBe('checking');
    calls[1]?.resolve(summaryFor('back'));
    await flush();

    expect(checker.state()).toEqual({ kind: 'ready', summary: summaryFor('back'), body: exampleBody() });
  });

  it('does not ask again about a setup it already shows', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(exampleBody());
    calls[0]?.resolve(summaryFor('once'));
    await flush();
    checker.show({ ...exampleBody() });

    expect(describe).toHaveBeenCalledTimes(1);
    expect(checker.state().kind).toBe('ready');
  });

  it('gets no answer after it is stopped', async () => {
    const { calls, describe } = fakeDescribe();
    const checker = new SetupSummaryChecker(describe, () => 0);

    checker.show(exampleBody());
    checker.stop();
    calls[0]?.resolve(summaryFor('late'));
    await flush();

    expect(checker.state().kind).toBe('checking');
  });
});
