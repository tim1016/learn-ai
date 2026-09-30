import { localWallClock, localWallClockMs } from '../../../shared/date/local-wall-clock';
import type { BotEndInput, BotEndView } from '../v2-panel/lib/broker-v2-panel.service';

/** Sell the bot's shares at its end, or keep them (#2607). */
export type BotEndAction = BotEndView['end_action'];

/**
 * A bot's end as the owner edits it: a date and a clock time in the viewer's
 * own zone, or no end at all, and what happens to its shares then. The end
 * itself travels as `int64 ms UTC` ({@link botEndInput}); these strings are
 * only what the date and time inputs hold.
 */
export interface BotEndFields {
  readonly date: string;
  readonly clock: string;
  readonly noEnd: boolean;
  readonly action: BotEndAction;
}

/**
 * The fields for an end the backend described. A "no end" keeps the date and
 * time the fields held before, so unticking it brings them back.
 */
export function botEndFields(
  end: Pick<BotEndView, 'end_at_ms' | 'end_action'>,
  previous: BotEndFields | null = null,
): BotEndFields {
  if (end.end_at_ms === null) {
    return { date: previous?.date ?? '', clock: previous?.clock ?? '', noEnd: true, action: end.end_action };
  }
  return { ...localWallClock(end.end_at_ms), noEnd: false, action: end.end_action };
}

/**
 * The end the fields ask for — always with its action, and "no end" as an
 * explicit `end_at_ms: null` — or `null` while the date or time is not a
 * real local wall clock yet. "No end" always sells: a bot with no end has no
 * shares to keep at one, and the backend refuses Keep there. Where Keep is
 * not offered (a Dry Run, which always sells at its end), the action is Sell
 * whatever was last chosen.
 */
export function botEndInput(fields: BotEndFields, keepOffered = true): BotEndInput | null {
  if (fields.noEnd) return { end_at_ms: null, end_action: 'SELL' };
  const endAtMs = localWallClockMs(fields);
  return endAtMs === null ? null : { end_at_ms: endAtMs, end_action: keepOffered ? fields.action : 'SELL' };
}
