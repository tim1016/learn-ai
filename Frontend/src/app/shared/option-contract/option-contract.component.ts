import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { formatNumber, formatPercent } from '@angular/common';
import { Tooltip } from 'primeng/tooltip';

import { AssetWordmarkComponent } from '../asset-identity/asset-wordmark.component';
import { formatTimestampDisplay } from '../timestamp/timestamp-display';

/** The facts that name one option contract, as the server sends them (#2913). */
export interface OptionContract {
  /** The contract's code at the broker, e.g. `SPY261009C00585000`. */
  occ_symbol: string;
  underlying: string;
  /** The expiry date's 16:00 New York anchor, `int64 ms UTC` (ADR 0022 (i)). */
  expiry_ms: number;
  right: 'call' | 'put';
  /** Dollars: the exact decimal the server sends (`"587.5"`), or a number. */
  strike: string | number;
}

/** Optional facts from the host's snapshot. A chip never fetches or estimates them. */
export interface OptionMarketDetails {
  bid?: string | number | null;
  ask?: string | number | null;
  last?: string | number | null;
  underlyingPrice?: string | number | null;
  delta?: number | null;
  ibkrDelta?: number | null;
  /** Annualized fraction, as supplied by the source. */
  impliedVolatility?: number | null;
  /** Only pass a known status belonging to this snapshot. */
  moneyness?: 'ITM' | 'ATM' | 'OTM' | null;
  asOfMs?: number | null;
  source?: string | null;
}

const RIGHT_IN_WORDS: Record<OptionContract['right'], string> = { call: 'Call', put: 'Put' };

/** Trim display zeros as text, so exact strikes never pass through a float. */
function strikeText(strike: string | number): string {
  const text = String(strike);
  return /^\d+\.\d+$/.test(text) ? text.replace(/\.?0+$/, '') : text;
}

function expiryText(contract: OptionContract): string {
  return formatTimestampDisplay(contract.expiry_ms, { mode: 'date-et', granularity: 'short-date' });
}

/** A contract's spoken name, including its year, for places no component can sit. */
export function optionContractName(contract: OptionContract): string {
  return `${contract.underlying} ${expiryText(contract)} ${strikeText(contract.strike)} ${RIGHT_IN_WORDS[contract.right]}`;
}

/** The shared compact contract identity. Extra facts stay in its hover card. */
@Component({
  selector: 'app-option-contract',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetWordmarkComponent, Tooltip],
  templateUrl: './option-contract.component.html',
  styleUrl: './option-contract.component.scss',
})
export class OptionContractComponent {
  readonly contract = input.required<OptionContract>();
  readonly market = input<OptionMarketDetails | null>(null);

  protected readonly name = computed(() => optionContractName(this.contract()));
  protected readonly expiry = computed(() => expiryText(this.contract()));
  protected readonly strike = computed(() => strikeText(this.contract().strike));
  protected readonly right = computed(() => RIGHT_IN_WORDS[this.contract().right]);
  // Display classification only; neither a price nor a Greek is estimated here.
  protected readonly moneyness = computed(() => {
    const market = this.market();
    if (market?.moneyness) return market.moneyness;
    const spot = Number(market?.underlyingPrice);
    const strike = Number(this.contract().strike);
    if (!Number.isFinite(spot) || spot <= 0 || !Number.isFinite(strike) || strike <= 0) return null;
    if (spot === strike) return 'ATM';
    const inTheMoney = this.contract().right === 'call' ? spot > strike : spot < strike;
    return inTheMoney ? 'ITM' : 'OTM';
  });
  protected readonly asOf = computed(() => {
    const ms = this.market()?.asOfMs;
    return ms == null ? null : formatTimestampDisplay(ms, { mode: 'local', granularity: 'datetime' });
  });
  protected readonly metrics = computed(() => {
    const market = this.market();
    if (!market) return [];
    const price = (value: string | number | null | undefined): string | null => {
      if (typeof value === 'number') return Number.isFinite(value) ? `$${formatNumber(value, 'en-US', '1.2-4')}` : null;
      return typeof value === 'string' && /^\d+(\.\d+)?$/.test(value) ? `$${value}` : null;
    };
    const decimal = (value: number | null | undefined): string | null =>
      value != null && Number.isFinite(value) ? formatNumber(value, 'en-US', '1.2-3') : null;
    const iv = market.impliedVolatility;
    return [
      { label: 'Bid', value: price(market.bid) },
      { label: 'Ask', value: price(market.ask) },
      { label: 'Last', value: price(market.last) },
      { label: 'Underlying', value: price(market.underlyingPrice) },
      { label: 'Delta', value: decimal(market.delta) },
      { label: 'IBKR delta', value: decimal(market.ibkrDelta) },
      { label: 'IV', value: iv != null && Number.isFinite(iv) ? formatPercent(iv, 'en-US', '1.1-1') : null },
    ].filter((metric) => metric.value !== null);
  });
}
