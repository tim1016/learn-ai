import type { BotDeployPrefill, DeployBotBody } from '../v2-panel/lib/broker-v2-panel.service';
import {
  freshOptionSettings,
  orderTermsSet,
  type DeployOptionSettings,
  type OptionOrderTermDefaults,
} from './option-structure/option-structure';

/**
 * What a bot trades, as the Deploy form holds it (#2912, #2913): one choice
 * of three, so a form can never hold a stock and an option structure at
 * once.
 *
 * `signal` is the shares of the symbol the bot listens to. "Another stock or
 * ETF" whose picker is still empty is `signal` too: nothing has been picked.
 * An option choice carries the form's own settings for it, whose order terms
 * are `null` until the catalogue's starting terms have been read.
 */
export type DeployTrades =
  | { readonly kind: 'signal' }
  | { readonly kind: 'stock'; readonly symbol: string }
  | ({ readonly kind: 'options' } & DeployOptionSettings);

export const TRADES_THE_SIGNAL: DeployTrades = { kind: 'signal' };

/** The other stock or ETF the bot trades, or `null` when it trades the signal's shares or options. */
export function tradedStock(trades: DeployTrades): string | null {
  return trades.kind === 'stock' ? trades.symbol : null;
}

/** The option settings the bot trades, or `null` for a bot that trades shares. */
export function tradedOptions(trades: DeployTrades): DeployOptionSettings | null {
  if (trades.kind !== 'options') return null;
  const { underlying, structure, legs, order_terms } = trades;
  return { underlying, structure, legs, order_terms };
}

/** Another stock or ETF, or back to the signal's own shares (`null`, or the signal's own symbol). */
export function tradesStock(symbol: string | null): DeployTrades {
  return symbol === null || symbol === '' ? TRADES_THE_SIGNAL : { kind: 'stock', symbol };
}

/** An option structure, under the form's settings for it. */
export function tradesOptions(options: DeployOptionSettings): DeployTrades {
  return { kind: 'options', ...options };
}

/** Options on `underlying`, before the owner has built anything. */
export function freshTradesOptions(underlying: string): DeployTrades {
  return tradesOptions(freshOptionSettings(underlying));
}

/**
 * The choice once the bot listens to `symbol`: a traded stock the signal has
 * caught up with is no longer a pick. An option choice stays as built.
 */
export function tradesListeningTo(trades: DeployTrades, symbol: string): DeployTrades {
  return trades.kind === 'stock' && trades.symbol === symbol ? TRADES_THE_SIGNAL : trades;
}

/** What a Deploy, a budget preview and a Start check are told of what the bot trades. */
export type TradesOnTheWire = Pick<DeployBotBody, 'traded_symbol' | 'trades'>;

/**
 * The choice as the three routes take it.
 *
 * A bot in shares sends what it always sent: nothing for the signal's own
 * shares, `traded_symbol` for another stock. The server keeps that field so
 * such a bot's request, and the fingerprint of it, are the bytes they were.
 * An option bot sends `trades` with its structure and all four order terms,
 * each percent as typed. `null` while its order terms cannot be sent: the
 * server fills none in.
 */
export function tradesOnTheWire(trades: DeployTrades, offered: OptionOrderTermDefaults | null): TradesOnTheWire | null {
  if (trades.kind === 'signal') return {};
  if (trades.kind === 'stock') return { traded_symbol: trades.symbol };
  const terms = trades.order_terms;
  if (!orderTermsSet(terms, offered)) return null;
  return {
    trades: {
      kind: 'options',
      underlying: trades.underlying,
      structure: trades.structure,
      legs: [...trades.legs],
      order_terms: {
        gap_limit_percent: terms.gap_limit_percent,
        entry_wait_seconds: terms.entry_wait_seconds,
        cost_limit_percent: terms.cost_limit_percent,
        expiry_close_lead_minutes: terms.expiry_close_lead_minutes,
      },
    },
  };
}

/**
 * What an earlier bot traded, for Deploy again: its option structure with
 * the legs and the four terms it was sealed with, its other stock, or the
 * signal's own shares.
 */
export function tradesOfPrefill(prefill: Pick<BotDeployPrefill, 'trades' | 'traded_symbol'>): DeployTrades {
  const sealed = prefill.trades ?? null;
  if (sealed === null) return tradesStock(prefill.traded_symbol?.trim().toUpperCase() || null);
  return {
    kind: 'options',
    underlying: sealed.underlying.trim().toUpperCase(),
    structure: sealed.structure ?? null,
    legs: sealed.legs,
    order_terms: { ...sealed.order_terms },
  };
}
