import { UNBOUNDED, heldBounds, type DateBounds } from '../date-picker';
import type { TickerCatalogView } from './ticker-catalog';

/**
 * The date bound of a run that reads ``view``'s tree as it stands: the days
 * the tree holds for ``symbol``.
 *
 * Not a verdict until the tree has answered. While its read is in flight, or
 * after it failed, the bound says so rather than "no held days".
 */
export function heldBoundsFor(view: TickerCatalogView, symbol: string): DateBounds {
  const unavailable = view.unavailable();
  if (unavailable !== null) return { ...UNBOUNDED, unknown: `The lake's holdings could not be read: ${unavailable}` };
  if (!view.resolved()) return { ...UNBOUNDED, unknown: "The lake's holdings are being read." };
  const held = view.pool().find((option) => option.symbol === symbol);
  return heldBounds(symbol, held?.firstHeld ?? null, held?.lastHeld ?? null);
}
