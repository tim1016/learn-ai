import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, inject, input, linkedSignal, model, output, untracked, viewChild } from '@angular/core';
import { FORM_FIELD, type FormValueControl } from '@angular/forms/signals';
import { Popover } from 'primeng/popover';

import type { IsoDate } from '../date/iso-date';
import { UNBOUNDED, type DateBounds } from './date-bounds';
import { DateCalendarComponent } from './date-calendar.component';
import { checkDate, fieldText, showFieldText } from './date-check';

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
 * the date in its own words. A host whose server also names the dates it
 * would take hands them over as {@link hostOffers}, one click each.
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
  /** Dates the host would take instead, such as the ones its server named with a refusal; each is offered as a button. */
  readonly hostOffers = input<readonly IsoDate[]>([]);
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
  /** The form field a host bound this control to with ``[formField]``; null for a host that binds the date itself. */
  private readonly formField = inject(FORM_FIELD, { optional: true, self: true });
  private readonly field = viewChild.required<ElementRef<HTMLInputElement>>('field');
  private readonly opener = viewChild.required<ElementRef<HTMLButtonElement>>('opener');
  private readonly popover = viewChild(Popover);

  protected readonly text = fieldText(this.value, () => this.emitted);
  /** Whether the in-place calendar is open. A field that is disabled, or enabled again, starts with it closed. */
  protected readonly open = linkedSignal({ source: this.disabled, computation: () => false });
  protected readonly entry = computed(() => checkDate(this.text(), { bounds: UNBOUNDED, sessions: null }));
  protected readonly calendarBounds = computed<DateBounds>(() => ({ ...UNBOUNDED, min: this.pickFrom() }));
  protected readonly problem = computed(() => (this.disabled() ? null : (this.entry().problem ?? this.hostProblem())));
  /** The host's offers the field can still take: none while disabled, and never the date it already holds. */
  protected readonly offers = computed(() => (this.disabled() ? [] : this.hostOffers().filter((date) => date !== this.entry().date)));

  constructor() {
    // A host that puts back a date this field has refused is answered with
    // the empty date again; reading its date tracked catches that.
    effect(() => {
      this.entry();
      this.value();
      this.disabled();
      untracked(() => this.handOver());
    });
    // #2866: Angular leaves a control showing its own value when its write and its host's write back land in one
    // task. Bound to a form, the field takes the form's date; this goes once signal forms record a control's own writes.
    const formField = this.formField;
    if (formField !== null) {
      effect(() => {
        const held = formField.state().controlValue();
        if (typeof held === 'string' && held !== untracked(this.value)) this.value.set(held);
      });
    }
    showFieldText(this.field, this.text);
  }

  focus(options?: FocusOptions): void {
    this.field().nativeElement.focus(options);
  }

  /** Takes a typed or picked date and hands it to the host at once, so a click in the same task already sees it. */
  protected apply(text: string): void {
    this.text.set(text.trim());
    this.handOver();
  }

  /** Takes a date the host offered. The keyboard moves to the field, because the button goes once the host accepts the date. */
  protected takeOffer(date: IsoDate): void {
    this.apply(date);
    this.focus();
  }

  protected toggleCalendar(event: Event, anchor: HTMLElement): void {
    if (this.calendar() === 'inline') this.open.update((open) => !open);
    else this.popover()?.toggle(event, anchor);
  }

  /** Puts the calendar away, e.g. when the host that shows this field closes. */
  closeCalendar(): void {
    this.open.set(false);
    this.popover()?.hide();
  }

  protected pick(date: IsoDate): void {
    if (this.disabled()) return;
    this.apply(date);
    this.dismissCalendar();
  }

  /** Puts the calendar away and hands the keyboard back to the button that opened it. */
  protected dismissCalendar(): void {
    this.closeCalendar();
    this.opener().nativeElement.focus();
  }

  private handOver(): void {
    if (this.disabled()) {
      // Nothing was handed over while disabled, so nothing the host now holds is this field's echo.
      this.emitted = null;
      return;
    }
    const { date } = this.entry();
    this.emitted = date;
    if (this.value() !== date) {
      this.value.set(date);
      // Read now, or the form never sees its date change when the host puts the previous one back in this task (#2866).
      this.formField?.state().value();
    }
  }
}
