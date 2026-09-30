import { ChangeDetectionStrategy, Component, ElementRef, computed, input, model, viewChild } from '@angular/core';
import { FormField, disabled, form, validate, type FieldState } from '@angular/forms/signals';

import { isCalendarDate, isClockMinute, localWallClockMs } from '../../../shared/date/local-wall-clock';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { BotEndFields } from './bot-end-fields';

let nextEditorId = 0;

/**
 * The fields of a bot's end (#2607), shared by the Deploy form and the bot
 * page: a date and a time in the viewer's own zone, with the same minute in
 * market time (ET) beside them; "No end — run until I stop it"; and whether
 * the bot sells or keeps its shares then.
 *
 * Keep is offered only where a bot may end holding: a Dry Run always sells
 * at the last price it saw, so its host passes `keepOffered` false and the
 * choice is not shown at all. The host owns the value and what it means;
 * this component only edits it, and says what a field needs once the owner
 * has left it (never per keystroke), or when the host asks
 * ({@link revealErrors}).
 */
@Component({
  selector: 'app-bot-end-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField, TimestampDisplayComponent],
  templateUrl: './bot-end-editor.component.html',
  styleUrl: './bot-end-editor.component.scss',
})
export class BotEndEditorComponent {
  readonly fields = model.required<BotEndFields>();
  readonly keepOffered = input(true);
  /** Holds every field still, e.g. while a change is being saved. */
  readonly locked = input(false);

  private readonly id = nextEditorId++;
  protected readonly marketId = `bot-end-editor-${this.id}-market`;
  protected readonly errorId = `bot-end-editor-${this.id}-error`;

  protected readonly endForm = form(this.fields, (end) => {
    disabled(end.date, { when: ({ valueOf }) => valueOf(end.noEnd) || this.locked() });
    disabled(end.clock, { when: ({ valueOf }) => valueOf(end.noEnd) || this.locked() });
    disabled(end.action, { when: ({ valueOf }) => valueOf(end.noEnd) || this.locked() });
    disabled(end.noEnd, { when: () => this.locked() });
    validate(end.date, ({ value, valueOf }) =>
      valueOf(end.noEnd) || isCalendarDate(value())
        ? undefined
        : { kind: 'bot-end-date', message: 'Enter the end’s date.' },
    );
    validate(end.clock, ({ value, valueOf }) => {
      if (valueOf(end.noEnd)) return undefined;
      if (!isClockMinute(value())) return { kind: 'bot-end-clock', message: 'Enter the end’s time.' };
      const date = valueOf(end.date);
      return !isCalendarDate(date) || localWallClockMs({ date, clock: value() }) !== null
        ? undefined
        : { kind: 'bot-end-clock', message: 'That time is skipped on that date where you are. Choose another.' };
    });
  });

  /** The minute the typed date and time name — said in market time beside
   * them — or `null` while they name none. */
  protected readonly typedAtMs = computed(() => {
    const fields = this.fields();
    return fields.noEnd ? null : localWallClockMs(fields);
  });

  protected readonly dateError = computed(() => shownError(this.endForm.date()));
  protected readonly clockError = computed(() => shownError(this.endForm.clock()));
  /** The one sentence under the fields: the date's problem, else the time's. */
  protected readonly error = computed(() => this.dateError() ?? this.clockError());

  private readonly dateInput = viewChild.required<ElementRef<HTMLInputElement>>('date');
  private readonly noEndInput = viewChild.required<ElementRef<HTMLInputElement>>('noEnd');

  /** Moves the keyboard to the first field that takes it, e.g. when the editor opens. */
  focus(): void {
    const date = this.dateInput().nativeElement;
    (date.disabled ? this.noEndInput().nativeElement : date).focus();
  }

  /** Says what each field needs now, e.g. when the owner asks to save fields that name no minute. */
  revealErrors(): void {
    this.endForm.date().markAsTouched();
    this.endForm.clock().markAsTouched();
  }
}

/** A field's problem once the owner has left it, or `null`. */
function shownError(field: FieldState<string>): string | null {
  return field.touched() && field.invalid() ? field.errors()[0]?.message ?? null : null;
}
