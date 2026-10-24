import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { ArchiveFactComponent } from './archive-fact.component';
import type { FactState } from './facts-board';
import { InstrumentFactsService } from './instrument-facts.service';
import { QuoteFactComponent } from './quote-fact.component';

/**
 * The facts an instrument's header states (PRD #3490 R08): who it is, its latest
 * price and day move with the basis and as-of time of each, how much of it the
 * lake holds, and what the options archive holds of it. Each is stated only when
 * a source gave it.
 *
 * The facts are read for the instrument and for nothing else. The chart window,
 * the timeframe and the tab are not inputs, so no change to them can move the
 * latest price or ask for it again; a price from inside a chart window is never
 * the latest price. Changing the instrument asks again and drops the old
 * instrument's answer at once, so one instrument's price is never shown under
 * another's name. A failed read says "unavailable" for the price and move and
 * leaves the identity, the held span and the page as they were.
 */
@Component({
  selector: 'app-instrument-facts-header',
  imports: [AssetIdentityComponent, QuoteFactComponent, ArchiveFactComponent],
  templateUrl: './instrument-facts-header.component.html',
  styleUrl: './instrument-facts-header.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InstrumentFactsHeaderComponent {
  private readonly service = inject(InstrumentFactsService);

  readonly symbol = input.required<string>();
  readonly name = input<string | null>(null);
  readonly firstHeld = input<string | null | undefined>(null);
  readonly lastHeld = input<string | null | undefined>(null);
  readonly delisted = input(false);

  /** Identity only in the params, so a reload keeps nothing stale and a new instrument starts clean. */
  private readonly read = resource({
    params: () => this.symbol(),
    loader: ({ params }) => this.service.read([params]),
  });

  protected readonly state = computed<FactState>(() => {
    if (this.read.isLoading()) return { kind: 'loading' };
    const read = this.read.value();
    if (read === undefined) return { kind: 'loading' };
    if (read.kind === 'failed') return { kind: 'failed', message: read.message };
    const facts = read.facts.get(this.symbol());
    return facts === undefined
      ? { kind: 'failed', message: `The answer left ${this.symbol()} out.` }
      : { kind: 'ready', facts };
  });

  protected readonly archive = computed(() => {
    const state = this.state();
    return state.kind === 'ready' ? state.facts.options_archive : null;
  });

  /** The held span, only when the lake gave both ends. */
  protected readonly span = computed(() => {
    const first = this.firstHeld();
    const last = this.lastHeld();
    return first == null || last == null ? null : `${first} to ${last}`;
  });

  protected readQuoteAgain(): void {
    this.read.reload();
  }
}
