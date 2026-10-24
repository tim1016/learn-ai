import { describe, expect, it } from 'vitest';

import { optionExpiryAnchorMs } from '../../../shared/option-contract/option-expiry-date';
import { chainModeOf, readChainRequest } from './chain-request';

const read = (query: string) => readChainRequest(new URLSearchParams(query));
const EXPIRY = optionExpiryAnchorMs('2026-10-16');
/** 16:00 ET on 2026-10-09 (UTC-4), the close of that session. */
const CLOSE = 1_791_576_000_000;

describe('what an address asks the chain for', () => {
  it('is Now · Snapshot when it names no source, with no expiry chosen for it', () => {
    expect(read('')).toEqual({ kind: 'snapshot', expiryMs: null });
    expect(chainModeOf(read(''))).toBe('now-snapshot');
  });

  it('keeps the expiry it names, whatever the source', () => {
    expect(read(`expiry=${EXPIRY}`)).toEqual({ kind: 'snapshot', expiryMs: EXPIRY });
    expect(read(`source=archive&expiry=${EXPIRY}`)).toMatchObject({ kind: 'archive', expiryMs: EXPIRY });
    expect(read(`source=live&expiry=${EXPIRY}`)).toEqual({ kind: 'live', expiryMs: EXPIRY });
  });

  it('is Past · Archive with the session date and the instant apart, either of which may be absent', () => {
    expect(read('source=archive')).toEqual({ kind: 'archive', expiryMs: null, date: null, atMs: null });
    expect(read('source=archive&date=2026-10-09')).toEqual({ kind: 'archive', expiryMs: null, date: '2026-10-09', atMs: null });
    expect(read(`source=archive&date=2026-10-09&at=${CLOSE}`)).toEqual({ kind: 'archive', expiryMs: null, date: '2026-10-09', atMs: CLOSE });
    expect(chainModeOf(read('source=archive'))).toBe('past-archive');
  });

  it('is Now · Live IBKR for the live source, and Past · Live IBKR when the live source is asked for a past date or instant', () => {
    expect(read('source=live')).toEqual({ kind: 'live', expiryMs: null });
    expect(read('source=live&date=2026-10-09')).toEqual({ kind: 'past-live', expiryMs: null, date: '2026-10-09', atMs: null });
    expect(read(`source=live&date=2026-10-09&at=${CLOSE}`)).toMatchObject({ kind: 'past-live', atMs: CLOSE });
    expect(chainModeOf(read('source=live'))).toBe('now-live');
    expect(chainModeOf(read('source=live&date=2026-10-09'))).toBe('past-live');
  });

  it.each([
    ['a source it does not offer', 'source=bogus', /The source "bogus" is not one this chain offers/],
    ['an expiry that is not milliseconds', 'expiry=2026-10-16', /The expiry "2026-10-16" is not a time in whole milliseconds/],
    ['an expiry past the last instant of the domain', 'expiry=253402300800000', /not a time in whole milliseconds/],
    ['a session date that is not a date', 'source=archive&date=2026-02-30', /The session date "2026-02-30" is not a date/],
    ['an instant that is not milliseconds', 'source=archive&date=2026-10-09&at=3pm', /The instant "3pm" is not a time in whole milliseconds/],
    ['an instant with no session date to fall on', `source=archive&at=${CLOSE}`, /A time of day needs the session date it falls on/],
    ['an instant on another New York date than the session date', `source=archive&date=2026-10-12&at=${CLOSE}`, /not on the session date 2026-10-12 in New York/],
    ['a session date asked of the current snapshot', 'source=snapshot&date=2026-10-09', /names the past, and Now · Snapshot is the current chain only/],
    ['an instant asked of the current snapshot, with no source named', `date=2026-10-09&at=${CLOSE}`, /names the past/],
  ])('says in place that it cannot be read when it names %s, and substitutes nothing', (_name, query, problem) => {
    const request = read(query);

    expect(request.kind).toBe('invalid');
    expect(request.kind === 'invalid' && request.problems.join(' ')).toMatch(problem);
  });

  it('keeps the source it did name, even when the rest cannot be read, so the strip says which one it was', () => {
    expect(chainModeOf(read('source=archive&date=nope'))).toBe('past-archive');
    expect(chainModeOf(read('source=live&expiry=x'))).toBe('now-live');
    expect(chainModeOf(read('source=bogus'))).toBeNull();
  });

  it('reads an instant on the session date in New York, not in UTC: 20:00 ET is the next UTC day', () => {
    // 20:00 EDT on 2026-10-09 is 00:00 UTC on 2026-10-10.
    expect(read('source=archive&date=2026-10-09&at=1791590400000')).toMatchObject({ kind: 'archive', date: '2026-10-09' });
  });
});
