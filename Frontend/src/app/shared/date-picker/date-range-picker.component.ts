import { ChangeDetectionStrategy, Component, computed, effect, inject, input, model, output, untracked } from '@angular/core';
import { Popover } from 'primeng/popover';
import { Tooltip } from 'primeng/tooltip';

import { UNBOUNDED, type DateBounds } from './date-bounds';
import { DateCalendarComponent } from './date-calendar.component';
import { checkRange, fieldText } from './date-check';
import { QUICK_RANGES, activeQuickRange, offerQuickRanges, type DateRange, type QuickRangeKey, type QuickRangeOffer } from './quick-ranges';
import { TradingCalendarService } from './trading-calendar.service';

let nextNoteId = 0;

/**
 * The one way a page picks a range of dates (#2858).
 *
 * Two typed fields share one calendar popup, with quick ranges underneath.
 * The dates are ET calendar dates (``YYYY-MM-DD``); the host converts them at
 * its own HTTP call, because its endpoint owns the ms anchor.
 *
 * A typed date applies on Enter or on leaving the field. Text that is not a
 * usable date stays in the field with the reason underneath, and the host
 * receives an empty date for that end, so its run stays disabled. Nothing is
 * corrected silently.
 */
@Component({
  selector: 'app-date-range-picker',
  imports: [Popover, Tooltip, DateCalendarComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './date-range-picker.component.html',
  styleUrl: './date-range-picker.component.scss',
})
export class DateRangePickerComponent {
  private readonly calendar = inject(TradingCalendarService);

  /** The range's first day; empty while the start field holds no usable date. */
  readonly from = model.required<string>();
  /** The range's last day, included; empty while the end field holds no usable date. */
  readonly to = model.required<string>();
  /** The days the host's run can read. */
  readonly bounds = input<DateBounds>(UNBOUNDED);
  /** The quick ranges offered, in ``QUICK_RANGES`` order. A host leaves out what makes no sense for it. */
  readonly ranges = input<readonly QuickRangeKey[]>(QUICK_RANGES);
  /**
   * Quick ranges a host's server has already resolved, shown in place of the
   * picker's own. For a host whose ranges are worked out server-side (Data
   * Lab's end on the session still forming); the labels stay the shared ones.
   */
  readonly hostOffers = input<readonly QuickRangeOffer[] | null>(null);
  /** Whether both ends must be trading sessions. */
  readonly sessionsOnly = input(true);
  /** The group's accessible name. */
  readonly label = input('Date range');
  /** Both dates after any change, in one event, for a host that keeps them in one value. */
  readonly changed = output<{ readonly from: string; readonly to: string }>();
  /** The quick range just clicked, for a host that does more on a click than take its dates. */
  readonly picked = output<QuickRangeKey>();

  protected readonly noteId = `date-range-note-${nextNoteId++}`;
  /** What this picker last handed its host, so its own empty date is not read back as the host clearing the field. */
  private emitted: { from: string | null; to: string | null } = { from: null, to: null };

  protected readonly fromText = fieldText(this.from, () => this.emitted.from);
  protected readonly toText = fieldText(this.to, () => this.emitted.to);

  protected readonly sessions = computed(() => (this.sessionsOnly() ? this.calendar.sessions() : null));
  /** Why the calendar could not be read, wherever this picker needs it: to check sessions, or to offer quick ranges. */
  protected readonly calendarProblem = computed(() => (this.sessionsOnly() || this.ranges().length > 0 ? this.calendar.problem() : null));
  protected readonly checked = computed(() => checkRange(this.fromText(), this.toText(), { bounds: this.bounds(), sessions: this.sessions() }));
  protected readonly problem = computed(() => this.checked().from.problem ?? this.checked().to.problem);
  protected readonly offers = computed(
    () => this.hostOffers() ?? offerQuickRanges(this.ranges(), this.bounds(), this.calendar.sessions(), this.calendar.lastCompleted(), this.sessionsOnly()),
  );
  protected readonly active = computed(() => activeQuickRange(this.offers(), this.checked().from.date, this.checked().to.date));

  /** How many sessions the range holds, and whether the page's data stops short of the market. */
  protected readonly summary = computed<string | null>(() => {
    const sessions = this.calendar.sessions();
    const { from, to } = this.checked();
    const parts: string[] = [];
    if (sessions !== null && from.date !== '' && to.date !== '') {
      const count = sessions.count(from.date, to.date);
      parts.push(`${count} ${count === 1 ? 'session' : 'sessions'}`);
    }
    const { max, maxReason } = this.bounds();
    const market = this.calendar.lastCompleted();
    if (max !== null && maxReason !== null && market !== null && max < market) parts.push(maxReason);
    return parts.length === 0 ? null : parts.join(' · ');
  });

  constructor() {
    // What the fields stand for can change without anyone typing: the bounds
    // or the calendar arrive, or the host puts back a date the field has
    // refused. Reading the host's dates tracked catches the last case.
    effect(() => {
      this.checked();
      this.from();
      this.to();
      untracked(() => this.handOver());
    });
  }

  /**
   * Hands the host what the fields stand for now. Called at once from every
   * action that changes a field, so a click that follows in the same task (a
   * touch tap on Run right after typing) already sees the new dates.
   */
  private handOver(): void {
    const { from, to } = this.checked();
    this.emitted = { from: from.date, to: to.date };
    if (this.from() === from.date && this.to() === to.date) return;
    this.from.set(from.date);
    this.to.set(to.date);
    this.changed.emit({ from: from.date, to: to.date });
  }

  protected applyFrom(text: string): void {
    this.fromText.set(text.trim());
    this.handOver();
  }

  protected applyTo(text: string): void {
    this.toText.set(text.trim());
    this.handOver();
  }

  protected setRange(range: DateRange): void {
    this.fromText.set(range.from);
    this.toText.set(range.to);
    this.handOver();
  }

  protected applyOffer(offer: QuickRangeOffer): void {
    if (offer.range === null) return;
    this.setRange(offer.range);
    this.picked.emit(offer.key);
  }

  protected retryCalendar(): void {
    this.calendar.retry();
  }
}
