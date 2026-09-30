import { ChangeDetectionStrategy, Component, ElementRef, input, model, viewChild } from '@angular/core';
import { FormField, disabled, form, validate } from '@angular/forms/signals';

import { localWallClockMs } from '../../../shared/date/local-wall-clock';
import type { BotEndFields } from './bot-end-fields';

/**
 * The fields of a bot's end (#2607), shared by the Deploy form and the bot
 * page: a date and a time in the viewer's own zone, "No end — run until I
 * stop it", and whether the bot sells or keeps its shares then.
 *
 * Keep is offered only where a bot may end holding: a Dry Run always sells
 * at the last price it saw, so its host passes `keepOffered` false and the
 * choice is not shown at all. The host owns the value and what it means;
 * this component only edits it, and says when the date or time is not a
 * real wall clock.
 */
@Component({
  selector: 'app-bot-end-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField],
  templateUrl: './bot-end-editor.component.html',
  styleUrl: './bot-end-editor.component.scss',
})
export class BotEndEditorComponent {
  readonly fields = model.required<BotEndFields>();
  readonly keepOffered = input(true);
  /** Holds every field still, e.g. while a change is being saved. */
  readonly locked = input(false);

  protected readonly endForm = form(this.fields, (end) => {
    disabled(end.date, { when: ({ valueOf }) => valueOf(end.noEnd) || this.locked() });
    disabled(end.time, { when: ({ valueOf }) => valueOf(end.noEnd) || this.locked() });
    disabled(end.action, { when: ({ valueOf }) => valueOf(end.noEnd) || this.locked() });
    disabled(end.noEnd, { when: () => this.locked() });
    validate(end.time, ({ valueOf }) =>
      valueOf(end.noEnd) || localWallClockMs({ date: valueOf(end.date), time: valueOf(end.time) }) !== null
        ? undefined
        : { kind: 'bot-end-wall-clock', message: 'Enter the end’s date and time.' },
    );
  });

  private readonly dateInput = viewChild.required<ElementRef<HTMLInputElement>>('date');
  private readonly noEndInput = viewChild.required<ElementRef<HTMLInputElement>>('noEnd');

  /** Moves the keyboard to the first field that takes it, e.g. when the editor opens. */
  focus(): void {
    const date = this.dateInput().nativeElement;
    (date.disabled ? this.noEndInput().nativeElement : date).focus();
  }
}
