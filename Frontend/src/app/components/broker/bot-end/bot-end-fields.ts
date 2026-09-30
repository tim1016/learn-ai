import { localWallClock, localWallClockMs } from '../../../shared/date/local-wall-clock';
import { refusalBody } from '../../../shared/errors/refusal-body';
import type { BotEndInput, BotEndView } from '../v2-panel/lib/broker-v2-panel.service';

/** Sell the bot's shares at its end, or keep them (#2607). */
export type BotEndAction = BotEndView['end_action'];

/**
 * A bot's end as the owner edits it: a date and a time in the viewer's own
 * zone, or no end at all, and what happens to its shares then. The end
 * itself travels as `int64 ms UTC` ({@link botEndInput}); these strings are
 * only what the date and time inputs hold.
 */
export interface BotEndFields {
  readonly date: string;
  readonly time: string;
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
    return { date: previous?.date ?? '', time: previous?.time ?? '', noEnd: true, action: end.end_action };
  }
  return { ...localWallClock(end.end_at_ms), noEnd: false, action: end.end_action };
}

/**
 * The end the fields ask for — always with its action, and "no end" as an
 * explicit `end_at_ms: null` — or `null` while the date or time is not a
 * real local wall clock yet. Where Keep is not offered (a Dry Run, which
 * always sells at its end), the action is Sell whatever was last chosen.
 */
export function botEndInput(fields: BotEndFields, keepOffered = true): BotEndInput | null {
  const endAction: BotEndAction = keepOffered ? fields.action : 'SELL';
  if (fields.noEnd) return { end_at_ms: null, end_action: endAction };
  const endAtMs = localWallClockMs(fields);
  return endAtMs === null ? null : { end_at_ms: endAtMs, end_action: endAction };
}

/** A refused end in the backend's own words, with its code for the receipt label. */
export interface BotEndRefusal {
  readonly message: string;
  readonly why: string | null;
  readonly nextAction: string | null;
  readonly reasonCode: string | null;
}

/**
 * A rejected end preview or change, read from whichever refusal shape
 * arrived — the panel's `{detail: {message, why, next_action, reason_code}}`
 * or the fleet coordinator's flat `{reason, message, next_step}` — or the
 * message of a change the page itself refused to send (a stale lane fence).
 * `fallback` is said only when nothing carried any words.
 */
export function botEndRefusal(error: unknown, fallback: string): BotEndRefusal {
  const body = refusalBody(error);
  const text = (key: string): string | null => {
    const value = body?.[key];
    return typeof value === 'string' && value.length > 0 ? value : null;
  };
  return {
    message: text('message') ?? (error instanceof Error ? error.message : fallback),
    why: text('why'),
    nextAction: text('next_action') ?? text('next_step'),
    reasonCode: text('reason_code') ?? text('reason'),
  };
}
