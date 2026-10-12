import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { InputText } from 'primeng/inputtext';

import { DateFieldComponent } from '../../../shared/date-picker';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { ChainControls } from './past-chain-controls';
import type { SessionBounds } from './past-chain.types';

/**
 * The four things the owner chooses: the underlying, the trading date, the time of day within that date's session, and
 * how many strikes to show each side of the money. The trading date and the expiry are separate controls: nothing here
 * writes the expiry, and the expiry is not read from here. A problem at a control is the server's sentence, or the
 * page's own when what was typed cannot be sent.
 */
@Component({
  selector: 'app-options-backtest-past-chain-controls',
  imports: [FormField, InputText, DateFieldComponent, SymbolPickerComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './past-chain-controls.component.html',
  styleUrls: ['../options-backtest-card.scss', './past-chain.scss'],
})
export class PastChainControlsComponent {
  readonly form = input.required<FieldTree<ChainControls>>();
  readonly underlyingProblem = input<string | null>(null);
  readonly dateProblem = input<string | null>(null);
  readonly timeProblem = input<string | null>(null);
  readonly strikesProblem = input<string | null>(null);
  /** The session of the picked date, once the server has said it. */
  readonly session = input<SessionBounds | null>(null);
  /** The number of strikes the server used last, from its answer: what an empty box means. */
  readonly strikesShown = input<number | null>(null);
  /** The most strikes each side the server takes, as digits, once the page has its limits. */
  readonly mostStrikes = input<string | null>(null);

  /** Enter in the strikes box counts what is typed at once, as leaving the box does. */
  protected commitStrikes(): void {
    this.form().strikes().markAsTouched();
  }
}
