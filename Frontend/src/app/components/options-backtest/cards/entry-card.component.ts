import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { InputText } from 'primeng/inputtext';

import type { SessionClock } from '../options-backtest.types';
import { distanceInWords, storedTime, type TimeRole } from '../session-time';
import { heldAnchorFor, type HeldAnchor, type SetupDraft } from '../setup-draft';
import { FIELD, NO_PROBLEMS, type FieldProblems } from '../setup-problems';
import { SwitchComponent } from '../controls/switch.component';

/**
 * When a setup trades: the entry time, the exit time, and the time after which
 * a stop earns no re-entry. The owner types New York clock times; beside each
 * the card says what is stored ("5 minutes after the open"), so a half day
 * keeps the setup's shape and the owner can see it does.
 */
@Component({
  selector: 'app-options-backtest-entry-card',
  imports: [FormField, InputText, SwitchComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './entry-card.component.html',
  styleUrl: '../options-backtest-card.scss',
})
export class EntryCardComponent {
  readonly form = input.required<FieldTree<SetupDraft>>();
  readonly clock = input.required<SessionClock>();
  readonly problems = input<FieldProblems>(NO_PROBLEMS);

  protected readonly field = FIELD;
  private readonly draft = computed(() => this.form()().value());
  protected readonly entryWords = computed(() => this.words('entry', this.draft().entryClock, this.draft().held.entry));
  protected readonly exitWords = computed(() => this.words('exit', this.draft().exitClock, this.draft().held.exit));
  protected readonly noReentryWords = computed(() =>
    this.words('noReentry', this.draft().noReentryClock, this.draft().held.noReentry),
  );

  private words(role: TimeRole, text: string, held: HeldAnchor | null): string | null {
    const stored = storedTime(role, text, this.clock(), heldAnchorFor(held, text));
    return stored.ok ? distanceInWords(stored.time) : null;
  }
}
