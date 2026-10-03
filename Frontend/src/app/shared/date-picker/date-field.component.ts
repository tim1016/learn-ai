import { ChangeDetectionStrategy, Component, computed, effect, inject, input, linkedSignal, model, untracked } from '@angular/core';
import { Popover } from 'primeng/popover';

import { UNBOUNDED, type DateBounds } from './date-bounds';
import { DateCalendarComponent } from './date-calendar.component';
import { checkDate } from './date-check';
import { TradingCalendarService } from './trading-calendar.service';

let nextNoteId = 0;

/**
 * The one way a page picks a single date (#2858): a typed field with a
 * calendar popup. It follows the same rules as the range picker's fields:
 * ``YYYY-MM-DD`` only, applied on Enter or on leaving the field, and an
 * unusable entry stays in the field with its reason while the host receives
 * an empty date.
 *
 * The date is a plain calendar day. What zone it lives in is the host's to
 * say: a trading date is an ET date, a bot's end date is the viewer's own.
 */
@Component({
  selector: 'app-date-field',
  imports: [Popover, DateCalendarComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './date-field.component.html',
  styleUrl: './date-field.component.scss',
})
export class DateFieldComponent {
  private readonly calendar = inject(TradingCalendarService);

  /** The date; empty while the field holds no usable one. */
  readonly value = model.required<string>();
  readonly bounds = input<DateBounds>(UNBOUNDED);
  /** Whether the date must be a trading session. Off by default: a month start may fall on a weekend. */
  readonly sessionsOnly = input(false);
  /** The field's accessible name, for a host with no visible ``<label>``. */
  readonly label = input<string | null>(null);
  /** The field's id: what a host's ``<label for>`` and its own messages point at. */
  readonly inputId = input<string | null>(null);
  /** A problem the host found with this date, such as a server refusal; shown as the field's own. */
  readonly hostProblem = input<string | null>(null);

  protected readonly noteId = `date-field-note-${nextNoteId++}`;
  private emitted: string | null = null;

  protected readonly text = linkedSignal<string, string>({
    source: this.value,
    computation: (value, previous) => (previous !== undefined && value === this.emitted ? previous.value : value),
  });

  protected readonly sessions = computed(() => (this.sessionsOnly() ? this.calendar.sessions() : null));
  protected readonly checked = computed(() => checkDate(this.text(), { bounds: this.bounds(), sessions: this.sessions() }));
  protected readonly problem = computed(() => this.checked().problem ?? this.hostProblem());

  constructor() {
    // Reads the host's date tracked: a host that puts back a date this field
    // has refused must be answered with the empty date again.
    effect(() => {
      const { date } = this.checked();
      const held = this.value();
      this.emitted = date;
      if (held !== date) untracked(() => this.value.set(date));
    });
  }
}
