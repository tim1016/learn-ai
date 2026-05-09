/**
 * Single seam between picker payloads (TickerRange / MultiTickerRange)
 * and the snake_case JSON shape every ``TickerRequest``-inheriting
 * Python route will accept (PR ii). The ``daily`` (UI) ↔ ``day``
 * (Polygon enum) translation lives here only — both stacks stay
 * idiomatic for their layer.
 *
 * Defaults:
 *   - ``multiplier`` defaults to 1 when undefined on the picker payload
 *   - ``session`` defaults to 'rth' when undefined
 *
 * Per-route default preservation (e.g. SignalEngineJobRequest's
 * pre-migration ``multiplier=15``) is the consumer's responsibility:
 * the consumer initializes the picker's ``range`` signal with the
 * desired default. The adapter is the seam, not the policy.
 */

import type {
  Resolution,
  TickerRange,
} from '../shared/ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from '../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';

export interface TickerRequestPayload {
  symbol: string;
  from_date: string;
  to_date: string;
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
  session: 'rth' | 'extended';
}

export type MultiTickerRequestPayload = Omit<TickerRequestPayload, 'symbol'> & {
  symbols: string[];
};

const RESOLUTION_TO_TIMESPAN: Readonly<
  Record<Resolution, 'minute' | 'hour' | 'day'>
> = {
  minute: 'minute',
  hour: 'hour',
  daily: 'day',
};

export function tickerRangeToWire(r: TickerRange): TickerRequestPayload {
  return {
    symbol: r.symbol,
    from_date: r.from,
    to_date: r.to,
    timespan: RESOLUTION_TO_TIMESPAN[r.resolution],
    multiplier: r.multiplier ?? 1,
    session: r.session ?? 'rth',
  };
}

export function multiTickerRangeToWire(
  r: MultiTickerRange,
): MultiTickerRequestPayload {
  return {
    symbols: [...r.symbols],
    from_date: r.from,
    to_date: r.to,
    timespan: RESOLUTION_TO_TIMESPAN[r.resolution],
    multiplier: r.multiplier ?? 1,
    session: r.session ?? 'rth',
  };
}
