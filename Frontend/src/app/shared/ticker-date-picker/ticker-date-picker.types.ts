/**
 * Types for the shared <app-ticker-date-picker>.
 *
 * Sibling for snapshot tools — single ticker + single date, no range,
 * no sampling. Used by ticker-explorer (option-expiration snapshots)
 * and any future "pick one ticker on one day" consumer.
 */

export interface TickerSnapshot {
  symbol: string;
  /** YYYY-MM-DD */
  date: string;
}
