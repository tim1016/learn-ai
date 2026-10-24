import type { AvailableQuote, InstrumentFacts, OptionsArchiveFact } from '../instrument-facts.service';

/** 2026-10-22 16:00 ET, the close the fixtures measure from. */
export const CLOSE_MS = Date.UTC(2026, 9, 22, 20, 0, 0);
/** 2026-10-23 10:45 ET, an observation a little before "now" in the fixtures. */
export const OBSERVED_MS = Date.UTC(2026, 9, 23, 14, 45, 0);

export function quote(overrides: Partial<AvailableQuote> = {}): AvailableQuote {
  return {
    status: 'available',
    price: 759.97,
    basis: 'delayed_snapshot',
    as_of_ms: OBSERVED_MS,
    move: { change: -6.92, change_percent: -0.9023, comparison_close: { price: 766.89, session_close_ms: CLOSE_MS } },
    move_unavailable_reason: null,
    not_used: [{ basis: 'live_quote', reason: 'No live line is streaming SPY in this process (line state: idle).' }],
    ...overrides,
  };
}

export function facts(symbol: string, quoteFact: InstrumentFacts['quote'] = quote(), archive: OptionsArchiveFact | null = null): InstrumentFacts {
  return { symbol, quote: quoteFact, options_archive: archive };
}

export function archive(overrides: Partial<OptionsArchiveFact> = {}): OptionsArchiveFact {
  return {
    kind: 'some_days',
    earliest_required_session_ms: Date.UTC(2025, 9, 20, 13, 30),
    last_archived_session_ms: Date.UTC(2026, 9, 21, 13, 30),
    complete_through_session_ms: null,
    missing_sessions: 4,
    capturing: true,
    reason: null,
    ...overrides,
  };
}

export const UNAVAILABLE: InstrumentFacts['quote'] = {
  status: 'unavailable',
  reason: 'No source supplied a price.',
  not_used: [
    { basis: 'live_quote', reason: 'No live line is streaming SPY in this process (line state: idle).' },
    { basis: 'delayed_snapshot', reason: 'The vendor snapshot could not be read (ConnectionError).' },
    { basis: 'last_close', reason: 'The lake holds no regular-hours bars for SPY on 2026-10-22.' },
  ],
};
