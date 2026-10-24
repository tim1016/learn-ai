import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { FactState } from './facts-board';
import type { AvailableQuote, QuoteBasis } from './instrument-facts.service';

/** What each basis is called where a price is shown: the name of the source, never a bare "last". */
export const BASIS_LABEL: Readonly<Record<QuoteBasis, string>> = {
  live_quote: 'Live quote',
  delayed_snapshot: 'Delayed snapshot',
  last_close: 'Last completed close',
};

/**
 * An instrument's latest price, its day move and where both came from (PRD
 * #3490 R08): the basis (live quote, delayed snapshot or last completed close),
 * the instant the price was observed, and the close the move is measured from.
 *
 * Presentation only: Python chose the basis and computed the move. A read that
 * failed, or a quote no source answered, says "unavailable" for the price and
 * the move and nothing else; the page around it is untouched. The figures here
 * are the instrument's latest price, which no chart window changes, so this
 * takes the instrument's own facts and no window.
 */
@Component({
  selector: 'app-quote-fact',
  imports: [DecimalPipe, TimestampDisplayComponent],
  templateUrl: './quote-fact.component.html',
  styleUrl: './quote-fact.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '[class.quote-fact--row]': 'layout() === "row"',
  },
})
export class QuoteFactComponent {
  readonly state = input.required<FactState>();
  readonly layout = input<'header' | 'row'>('header');

  /** The user asked for the price to be read again after it could not be. */
  readonly retry = output();

  protected readonly quote = computed<AvailableQuote | null>(() => {
    const state = this.state();
    return state.kind === 'ready' && state.facts.quote.status === 'available' ? state.facts.quote : null;
  });

  protected readonly loading = computed(() => this.state().kind === 'loading');

  /** Why there is no price, in the words of the failed read or of each source that was asked. */
  protected readonly unavailable = computed<readonly string[] | null>(() => {
    const state = this.state();
    if (state.kind === 'failed') return [state.message];
    if (state.kind === 'ready' && state.facts.quote.status === 'unavailable') {
      return state.facts.quote.not_used.map((item) => `${BASIS_LABEL[item.basis]}: ${item.reason}`);
    }
    return null;
  });

  protected readonly basisLabel = computed(() => {
    const quote = this.quote();
    return quote === null ? '' : BASIS_LABEL[quote.basis];
  });

  /** The reason the freshest source was passed over, so a price that is not live says so. */
  protected readonly notLive = computed(() => {
    const quote = this.quote();
    return quote === null || quote.basis === 'live_quote' ? null : (quote.not_used[0]?.reason ?? null);
  });

  protected readonly moveTone = computed(() => {
    const change = this.quote()?.move?.change ?? 0;
    return change > 0 ? 'positive' : change < 0 ? 'negative' : 'flat';
  });

  protected readonly moveSign = computed(() => ((this.quote()?.move?.change ?? 0) > 0 ? '+' : ''));

  /** Every source's reason on the element's tooltip, for a reader who wants the whole account. */
  protected readonly detail = computed(() => {
    const quote = this.quote();
    if (quote !== null) return quote.not_used.map((item) => `${BASIS_LABEL[item.basis]}: ${item.reason}`).join('\n');
    return (this.unavailable() ?? []).join('\n');
  });
}
