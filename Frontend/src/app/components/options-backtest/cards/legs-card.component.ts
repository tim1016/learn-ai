import { ChangeDetectionStrategy, Component, computed, input, output, viewChildren } from '@angular/core';
import type { FieldTree } from '@angular/forms/signals';

import type { SetupLimits } from '../options-backtest.types';
import type { SetupDraft } from '../setup-draft';
import { legsFull } from '../setup-limits';
import { FIELD, NO_PROBLEMS, type FieldProblems } from '../setup-problems';
import { LegCardComponent } from './leg-card.component';

/** The legs of the setup, one numbered card each. Copy and delete are the owner's intent; the page carries them out. */
@Component({
  selector: 'app-options-backtest-legs-card',
  imports: [LegCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './legs-card.component.html',
  styleUrls: ['../options-backtest-card.scss', './legs-card.component.scss'],
})
export class LegsCardComponent {
  readonly form = input.required<FieldTree<SetupDraft>>();
  readonly problems = input<FieldProblems>(NO_PROBLEMS);
  /** What the server allows; null while it is not known, and then the server judges. */
  readonly limits = input<SetupLimits | null>(null);
  readonly legCopied = output<string>();
  readonly legRemoved = output<string>();

  private readonly cards = viewChildren(LegCardComponent);

  /** Puts the keyboard on the delete control of the leg at that position, if there is one. */
  focusDelete(position: number): void {
    this.cards()[position]?.focusDelete();
  }

  /** Puts the keyboard on the heading of the leg at that position, if there is one. */
  focusHeading(position: number): void {
    this.cards()[position]?.focusHeading();
  }

  protected readonly maxLegs = computed(() => this.limits()?.max_legs ?? null);
  protected readonly legs = computed(() => this.form().legs().value());
  protected readonly full = computed(() => legsFull(this.legs().length, this.limits()));
  /** With no legs the card already says to add one; the server's reason is shown once there are legs it can be about. */
  protected readonly legsProblem = computed(() => (this.legs().length === 0 ? null : (this.problems().get(FIELD.legs) ?? null)));
}
