import type { NewsQuery } from '../../../services/news.service';
import type { DataLabWindowMsUtc } from '../data-lab-workspace-store';

/* App-owned numeric-window → vendor-query adapter (PRD §11).
 *
 * The Data Lab UI owns an int64 ms UTC window and must not construct vendor
 * date strings inline. `NewsService`'s wire contract is Polygon's
 * `/v2/reference/news` string operators, so this single pure helper is the
 * only place that converts the committed numeric window into the vendor
 * parameters: inclusive start via `published_utc_gte`, exclusive end via
 * `published_utc_lt`. */

/** Format an int64 ms UTC instant as the vendor's RFC-3339 UTC string. Pure. */
export function utcMsToIsoInstant(msUtc: number): string {
  return new Date(msUtc).toISOString();
}

export const NEWS_MAX_HEADLINES = 5;

/** Map the committed numeric UTC window and ticker to the news query.
 *  `endMsUtc` is exclusive (PRD §11). Pure. */
export function windowToNewsQuery(
  ticker: string,
  window: DataLabWindowMsUtc,
  limit: number = NEWS_MAX_HEADLINES,
): NewsQuery {
  return {
    ticker,
    published_utc_gte: utcMsToIsoInstant(window.startMsUtc),
    published_utc_lt: utcMsToIsoInstant(window.endMsUtc),
    order: 'desc',
    sort: 'published_utc',
    limit,
  };
}
