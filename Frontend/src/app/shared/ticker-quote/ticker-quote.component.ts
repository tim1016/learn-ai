import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  LOCALE_ID,
} from '@angular/core';
import { DecimalPipe, formatNumber } from '@angular/common';
import { AssetIdentityComponent } from '../asset-identity';

export interface TickerQuoteView {
  ticker: string;
  name?: string | null;
  exchange?: string | null;
  price: number;
  change?: number | null;
  changePercent: number | null;
  logoSlug?: string | null;
  currencySymbol?: string;
}

@Component({
  selector: 'app-ticker-quote',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, DecimalPipe],
  templateUrl: './ticker-quote.component.html',
  styleUrl: './ticker-quote.component.scss',
  host: {
    '[class.ticker-quote--inline]': 'mode() === "inline"',
    '[class.ticker-quote--card]': 'mode() === "card"',
    '[class.ticker-quote--inverse]': 'tone() === "inverse"',
    '[attr.title]': 'hostTitle()',
  },
})
export class TickerQuoteComponent {
  private readonly locale = inject(LOCALE_ID);

  readonly quote = input.required<TickerQuoteView>();
  readonly mode = input<'inline' | 'card'>('inline');
  readonly size = input<'sm' | 'md' | 'lg'>('sm');
  readonly tone = input<'default' | 'inverse'>('default');
  readonly logo = input<boolean>(true);

  readonly isCard = computed(() => this.mode() === 'card');

  readonly effectiveSize = computed(() => this.size());

  readonly currencySymbol = computed(() => this.quote().currencySymbol ?? '$');

  readonly trend = computed(() => {
    const pct = this.quote().changePercent;
    if (pct === null) return 'unavailable' as const;
    if (pct > 0) return 'positive' as const;
    if (pct < 0) return 'negative' as const;
    return 'flat' as const;
  });

  readonly signPrefix = computed(() =>
    (this.quote().changePercent ?? 0) > 0 ? '+' : '',
  );

  readonly caretClass = computed(() => {
    const t = this.trend();
    if (t === 'positive') return 'pi pi-caret-up';
    if (t === 'negative') return 'pi pi-caret-down';
    return 'pi pi-minus';
  });

  readonly changeAriaLabel = computed(() => {
    const pct = this.quote().changePercent;
    if (pct === null) return 'change unavailable';
    const t = this.trend();
    const abs = Math.abs(pct).toFixed(2);
    if (t === 'positive') return `up ${abs} percent`;
    if (t === 'negative') return `down ${abs} percent`;
    return 'unchanged';
  });

  readonly identityName = computed(() =>
    this.isCard() ? (this.quote().name ?? null) : null,
  );

  readonly identityExchange = computed(() =>
    this.isCard() ? (this.quote().exchange ?? null) : null,
  );

  readonly hostTitle = computed(() => {
    const q = this.quote();
    const name = q.name?.trim() || null;
    const currency = this.currencySymbol();
    const sign = this.signPrefix();
    const priceStr = `${currency}${formatNumber(q.price, this.locale, '1.2-2')}`;
    const changeStr = q.change === null || q.change === undefined
      ? ''
      : ` (${q.change < 0 ? '-' : sign}${currency}${formatNumber(Math.abs(q.change), this.locale, '1.2-2')})`;
    const pctStr = q.changePercent === null
      ? 'change unavailable'
      : `${sign}${formatNumber(q.changePercent, this.locale, '1.2-2')}%`;
    const identity = name ? `${name} (${q.ticker})` : q.ticker;
    return `${identity} — ${priceStr}${changeStr}, ${pctStr}`;
  });
}
