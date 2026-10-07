import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { DeployOptionOrderTermFieldsComponent } from './deploy-option-order-term-fields.component';
import type { OptionOrderTermDefaults, OptionOrderTerms } from './option-structure/option-structure';

/**
 * Deploy's option order terms (#2915), which stand where the stock exit
 * terms stand for a bot that trades options: the widest bid-ask gap a leg
 * may have, how long an entry waits, the most opening and closing at once
 * may cost, and how long before the close an expiry-day position is closed.
 *
 * The terms come with the catalogue of what the builder offers. Until it has
 * been read this says so; once the read has failed it says why, in the words
 * the What step shows beside its retry, and no longer that it is reading.
 */
@Component({
  selector: 'app-deploy-option-order-terms',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DeployOptionOrderTermFieldsComponent],
  templateUrl: './deploy-option-order-terms.component.html',
  styleUrl: './deploy-option-order-terms.component.scss',
})
export class DeployOptionOrderTermsComponent {
  /** The owner's terms; `null` until the catalogue's starting terms have been read. */
  readonly terms = input.required<OptionOrderTerms | null>();
  /** The catalogue's labels, units and starting values; `null` until it has been read. */
  readonly defaults = input.required<OptionOrderTermDefaults | null>();
  /** Why the catalogue could not be read, when it could not. */
  readonly failure = input<string | null>(null);

  readonly termsChange = output<OptionOrderTerms>();

  /** The terms and what the catalogue says of them, once both are there. */
  protected readonly read = computed(() => {
    const terms = this.terms();
    const defaults = this.defaults();
    return terms === null || defaults === null ? null : { terms, defaults };
  });
}
