import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';
import type { MoneySegment } from '../../broker/v2-panel/lib/broker-v2-panel.service';

/** One bot on Home: its roster row and, joined by `strategy_instance_id`, its
 * own slice of the account's money bar — `null` when the bar has none for it
 * (a Dry Run, a Finished bot, or a money read that is not ready). */
export interface HomeBot {
  readonly bot: BotCatalogView;
  readonly slice: MoneySegment | null;
}

/** Home's bot list, in the groups the backend authored (PRD #2560 D5/D7).
 * The List and the Wall both render exactly these arrays, so they can never
 * show the bots in two orders. */
export interface HomeBots {
  readonly running: readonly HomeBot[];
  readonly holding: readonly HomeBot[];
  readonly dryRun: readonly HomeBot[];
  readonly finished: readonly BotCatalogView[];
}

/**
 * Group the roster and join each row to its money slice.
 *
 * A join, never arithmetic: every dollar a row shows is its own slice's
 * Python-authored string. Groups keep the backend's order; Finished lists the
 * most recently ended first.
 */
export function homeBots(catalog: readonly BotCatalogView[], segments: readonly MoneySegment[]): HomeBots {
  const slices = new Map<string, MoneySegment>();
  for (const segment of segments) {
    if (segment.strategy_instance_id) slices.set(segment.strategy_instance_id, segment);
  }
  const withSlice = (bot: BotCatalogView): HomeBot => ({
    bot,
    slice: slices.get(bot.strategy_instance_id) ?? null,
  });
  const inGroup = (group: BotCatalogView['group']) =>
    catalog.filter((bot) => bot.group === group);
  return {
    running: inGroup('running').map(withSlice),
    holding: inGroup('holding').map(withSlice),
    dryRun: inGroup('dry_run').map(withSlice),
    finished: [...inGroup('finished')].sort(
      (left, right) => (right.ended_at_ms ?? 0) - (left.ended_at_ms ?? 0),
    ),
  };
}
