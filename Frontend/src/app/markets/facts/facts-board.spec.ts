import { describe, expect, it, vi } from 'vitest';

import { FactsBoard } from './facts-board';
import { MAX_FACT_SYMBOLS, type FactsRead } from './instrument-facts.service';
import { facts } from './testing/facts-fixtures';

function service(answer: (symbols: readonly string[]) => Promise<FactsRead>) {
  const read = vi.fn(answer);
  return { read, board: new FactsBoard({ read }) };
}

const answering = async (symbols: readonly string[]): Promise<FactsRead> => ({
  kind: 'ok',
  facts: new Map(symbols.map((symbol) => [symbol, facts(symbol)])),
});

const symbols = (count: number) => Array.from({ length: count }, (_, index) => `S${index}`);

describe('FactsBoard', () => {
  it('reads a symbol as loading until its batch answers, then as its facts', async () => {
    const { board } = service(answering);

    board.ask(['SPY']);
    expect(board.stateOf('SPY').kind).toBe('loading');

    await vi.waitFor(() => expect(board.stateOf('SPY').kind).toBe('ready'));
  });

  it('asks for each symbol once however often it is asked, so a redraw reads nothing', async () => {
    const { board, read } = service(answering);

    board.ask(symbols(20));
    board.ask(symbols(20));
    await vi.waitFor(() => expect(board.stateOf('S19').kind).toBe('ready'));
    board.ask(symbols(20));

    expect(read).toHaveBeenCalledTimes(1);
  });

  it('asks only for the rows a page adds, never again for the rows already drawn', async () => {
    const { board, read } = service(answering);

    board.ask(symbols(20));
    board.ask(symbols(40));

    expect(read.mock.calls.map(([batch]) => batch.length)).toEqual([20, 20]);
    expect(read.mock.calls[1][0]).toEqual(symbols(40).slice(20));
  });

  it('splits what it asks for at the server\'s bound, one request per batch', () => {
    const { board, read } = service(answering);

    board.ask(symbols(MAX_FACT_SYMBOLS * 2 + 10));

    expect(read.mock.calls.map(([batch]) => batch.length)).toEqual([MAX_FACT_SYMBOLS, MAX_FACT_SYMBOLS, 10]);
  });

  it('marks every symbol of a failed read failed, says why, and reads again only those on retry', async () => {
    let healthy = false;
    const { board, read } = service(async (batch) => (healthy ? answering(batch) : { kind: 'failed', message: 'The data plane is down.' }));
    board.ask(['SPY', 'QQQ']);
    await vi.waitFor(() => expect(board.hasFailed()).toBe(true));
    expect(board.stateOf('SPY')).toEqual({ kind: 'failed', message: 'The data plane is down.' });

    healthy = true;
    board.retry();
    await vi.waitFor(() => expect(board.stateOf('SPY').kind).toBe('ready'));

    expect(read.mock.calls.map(([batch]) => batch)).toEqual([['SPY', 'QQQ'], ['SPY', 'QQQ']]);
    expect(board.hasFailed()).toBe(false);
    board.retry();
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('fails only the symbol an answer leaves out', async () => {
    const { board } = service(async () => ({ kind: 'ok', facts: new Map([['SPY', facts('SPY')]]) }));

    board.ask(['SPY', 'QQQ']);

    await vi.waitFor(() => expect(board.stateOf('QQQ').kind).toBe('failed'));
    expect(board.stateOf('SPY').kind).toBe('ready');
  });
});
