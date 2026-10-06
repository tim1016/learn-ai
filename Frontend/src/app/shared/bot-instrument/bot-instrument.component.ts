import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { components } from '../../api/broker.types';
import {
  AssetIdentityComponent,
  type AssetIdentitySize,
} from '../asset-identity/asset-identity.component';

export type TradedInstrument = components['schemas']['TradedInstrumentView'];

/**
 * The symbol a bot's orders and positions are in: what it trades, which is
 * the symbol it listens to unless the server says it trades something else.
 * For a reader that asks what the bot holds, never what it listens to.
 */
export function orderSymbol(symbol: string, traded: TradedInstrument | null | undefined): string {
  return traded ? traded.symbol : symbol;
}

/** `arrow` for a line in a list of bots; `words` where the page has room to say it. */
export type BotInstrumentForm = 'arrow' | 'words';

/**
 * What a bot listens to and, when that is something else, what it trades.
 *
 * The one renderer every list of bots uses. A bot that trades the shares of
 * the symbol it listens to shows that symbol alone, as it always has. A bot
 * that trades something else shows both: `SPY → QQQ` in a list, or
 * "Listens to SPY · Trades QQQ" where there is room. Whether a bot trades
 * something else is the server's fact (`traded` is present); this component
 * never compares two tickers.
 */
@Component({
  selector: 'app-bot-instrument',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent],
  templateUrl: './bot-instrument.component.html',
  styleUrl: './bot-instrument.component.scss',
  host: {
    '[attr.role]': "traded() ? 'group' : null",
    '[attr.aria-label]': 'spokenName()',
    '[attr.title]': 'spokenName()',
  },
})
export class BotInstrumentComponent {
  /** The Signal stream: what the bot listens to. */
  readonly symbol = input.required<string>();
  /** What it trades, when the server says that is something else. */
  readonly traded = input<TradedInstrument | null | undefined>(null);
  readonly form = input<BotInstrumentForm>('arrow');
  readonly size = input<AssetIdentitySize>('xs');

  /** The full words, so the arrow is never a bare symbol to a screen reader or on hover. */
  protected readonly spokenName = computed(() => {
    const traded = this.traded();
    return traded ? `Listens to ${this.symbol()}, trades ${traded.symbol}` : null;
  });
}
