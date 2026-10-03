import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, input, model, output, signal, untracked, viewChild } from '@angular/core';
import type { FormValueControl } from '@angular/forms/signals';
import { Popover } from 'primeng/popover';

import type { IsoDate } from '../date/iso-date';
import { UNBOUNDED, type DateBounds } from './date-bounds';
import { DateCalendarComponent } from './date-calendar.component';
import { checkDate, fieldText } from './date-check';

let nextNoteId = 0;

/**
 * The one way a page picks a single date (#2858): a typed field with a
 * calendar. It follows the same rules as the range picker's fields:
 * ``YYYY-MM-DD`` only, applied on Enter or on leaving the field, and an
 * unusable entry stays in the field with its reason while the host receives
 * an empty date.
 *
 * The date is a plain calendar day, any day of the week. What zone it lives
 * in is the host's to say: a trading date is an ET date, a bot's end date is
 * the viewer's own. What makes a date wrong for the host is the host's to
 * say too, through {@link hostProblem}: both hosts have a server that judges
 * the date in its own words.
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
  /** The date; empty while the field holds no usable one. */
  readonly value = model.required<string>();
  /** The first day the calendar offers. Earlier days can still be shown and typed: the host's server judges them. */
  readonly pickFrom = input<IsoDate | null>(null);
  /** The field's id: what a host's ``<label for>`` and its own messages point at. */
  readonly inputId = input<string | null>(null);
  /** A problem the host found with this date, such as a server refusal; shown as the field's own. */
  readonly hostProblem = input<string | null>(null);
  /** ``filled`` among a form's filled fields; ``outlined`` everywhere else. */
  readonly appearance = input<'outlined' | 'filled'>('outlined');
  /**
   * Where the calendar opens. ``popup`` floats over the page. ``inline``
   * opens it in place under the field, for a host that is itself a native
   * popover: a floating calendar would paint beneath the top layer, and a
   * click on it would count as outside and dismiss the host.
   */
  readonly calendar = input<'popup' | 'inline'>('popup');
  /** A disabled field checks nothing and hands nothing back: its date is not in use. */
  readonly disabled = input(false);
  /** The field has been left. */
  readonly touch = output();

  protected readonly noteId = `date-field-note-${nextNoteId++}`;
  private emitted: string | null = null;
  private readonly field = viewChild.required<ElementRef<HTMLInputElement>>('field');
  private readonly popover = viewChild(Popover);

  protected readonly text = fieldText(this.value, () => this.emitted);
  /** Whether the in-place calendar is open. */
  protected readonly open = signal(false);
  protected readonly entry = computed(() => checkDate(this.text(), { bounds: UNBOUNDED, sessions: null }));
  protected readonly calendarBounds = computed<DateBounds>(() => ({ ...UNBOUNDED, min: this.pickFrom() }));
  protected readonly problem = computed(() => (this.disabled() ? null : (this.entry().problem ?? this.hostProblem())));

  constructor() {
    // A host that puts back a date this field has refused is answered with
    // the empty date again; reading its date tracked catches that.
    effect(() => {
      this.entry();
      this.value();
      this.disabled();
      untracked(() => this.handOver());
    });
  }

  focus(options?: FocusOptions): void {
    this.field().nativeElement.focus(options);
  }

  /** Takes a typed or picked date and hands it to the host at once, so a click in the same task already sees it. */
  protected apply(text: string): void {
    this.text.set(text.trim());
    this.handOver();
  }

  protected toggleCalendar(event: Event, anchor: HTMLElement): void {
    if (this.calendar() === 'inline') this.open.update((open) => !open);
    else this.popover()?.toggle(event, anchor);
  }

  protected pick(date: IsoDate): void {
    this.apply(date);
    this.open.set(false);
    this.popover()?.hide();
  }

  private handOver(): void {
    if (this.disabled()) {
      // Nothing was handed over while disabled, so nothing the host now holds is this field's echo.
      this.emitted = null;
      return;
    }
    const { date } = this.entry();
    this.emitted = date;
    if (this.value() !== date) this.value.set(date);
  }
}
