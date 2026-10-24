import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ExpiryTagComponent } from './archive/expiry-tag.component';
import { chainAbsentWords, type ChainFact, type ChainField } from './chain-presentation';

/**
 * What a chain says about itself before its rows: which source it is, the instant it is as of,
 * whether its quotes are delayed or stale, the underlying and the expiry it is of, and what the
 * source cannot say. Each is a typed fact the source adapter wrote; this prints them and adds
 * no claim of its own.
 */
@Component({
  selector: 'app-chain-facts',
  imports: [TimestampDisplayComponent, ExpiryTagComponent],
  templateUrl: './chain-facts.component.html',
  styleUrl: './chain-facts.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChainFactsComponent {
  readonly facts = input.required<readonly ChainFact[]>();
  /** Sentences for what this source cannot say. */
  readonly notes = input<readonly string[]>([]);

  protected wordsOf(field: Extract<ChainField, { kind: 'absent' }>): string {
    return chainAbsentWords(field);
  }
}
