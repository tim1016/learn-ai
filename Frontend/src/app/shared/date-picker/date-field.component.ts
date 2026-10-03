import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, inject, input, linkedSignal, model, output, untracked, viewChild } from '@angular/core';
import type { FormValueControl } from '@angular/forms/signals';
import { Popover } from 'primeng/popover';

import type { IsoDate } from '../date/iso-date';
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
 *
 * It is a signal-forms control: a host binds it with ``[formField]`` and it
 * takes the field's disabled state and reports when it has been left.
 */
@Component({
  selector: 'app-date-field',
  imports: [Popover, DateCalendarComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './date-field.component.html',
  styleUrl: './date-field.component.scss',
  host: { '[class.date-field--filled]': "appearance() === 'filled'" },
})
export class DateFieldComponent implements FormValueControl<string> {
  private readonly calendar = inject(TradingCalendarService);

  /** The date; empty while the field holds no usable one. */
  readonly value = model.required<string>();
  readonly bounds = input<DateBounds>(UNBOUNDED);
  /**
   * The first day the calendar offers, for a host whose server judges the
   * date: earlier days can still be shown and typed, so a date that has
   * passed keeps the server's own words about it.
   */
  readonly pickFrom = input<IsoDate | null>(null);
  /** Whether the date must be a trading session. Off by default: a month start may fall on a weekend. */
  readonly sessionsOnly = input(false);
  /** The field's accessible name, for a host with no visible ``<label>``. */
  readonly label = input<string | null>(null);
  /** The field's id: what a host's ``<label for>`` and its own messages point at. */
  readonly inputId = input<string | null>(null);
  /** A problem the host found with this date, such as a server refusal; shown as the field's own. */
  readonly hostProblem = input<string | null>(null);
  /** ``filled`` among a form's filled fields; ``outlined`` everywhere else. */
  readonly appearance = input<'outlined' | 'filled'>('outlined');
  /** A disabled field checks nothing and hands nothing back: its date is not in use. */
  readonly disabled = input(false);
  /** The field has been left. */
  readonly touch = output();

  private readonly field = viewChild.required<ElementRef<HTMLInputElement>>('field');

  protected readonly noteId = `date-field-note-${nextNoteId++}`;
  private emitted: string | null = null;

  protected readonly text = linkedSignal<string, string>({
    source: this.value,
    computation: (value, previous) => (previous !== undefined && value === this.emitted ? previous.value : value),
  });

  protected readonly sessions = computed(() => (this.sessionsOnly() ? this.calendar.sessions() : null));
  protected readonly entry = computed(() => checkDate(this.text(), { bounds: this.bounds(), sessions: this.sessions() }));
  protected readonly calendarBounds = computed<DateBounds>(() => {
    const bounds = this.bounds();
    return { ...bounds, min: this.pickFrom() ?? bounds.min };
  });
  protected readonly problem = computed(() => (this.disabled() ? null : (this.entry().problem ?? this.hostProblem())));

  constructor() {
    // Reads the host's date tracked: a host that puts back a date this field
    // has refused must be answered with the empty date again.
    effect(() => {
      if (this.disabled()) return;
      const { date } = this.entry();
      const held = this.value();
      this.emitted = date;
      if (held !== date) untracked(() => this.value.set(date));
    });
  }

  focus(options?: FocusOptions): void {
    this.field().nativeElement.focus(options);
  }
}
