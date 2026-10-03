import { ChangeDetectionStrategy, Component, computed, effect, inject, input, linkedSignal, model, untracked } from '@angular/core';
import { Popover } from 'primeng/popover';
import { Tooltip } from 'primeng/tooltip';

import { UNBOUNDED, type DateBounds } from './date-bounds';
import { DateCalendarComponent } from './date-calendar.component';
import { checkRange } from './date-check';
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
  /** Whether both ends must be trading sessions. */
  readonly sessionsOnly = input(true);
  /**
   * The quick range to start on when the host hands over no dates. A host
   * with no window of its own starts empty and names one, so its first
   * window comes from the calendar rather than from day arithmetic that
   * cannot see a holiday.
   */
  readonly fallback = input<QuickRangeKey | null>(null);
  /** The group's accessible name. */
  readonly label = input('Date range');

  protected readonly noteId = `date-range-note-${nextNoteId++}`;
  /** What this picker last handed its host, so its own empty date is not read back as the host clearing the field. */
  private emitted: { from: string | null; to: string | null } = { from: null, to: null };

  protected readonly fromText = linkedSignal<string, string>({
    source: this.from,
    computation: (value, previous) => (previous !== undefined && value === this.emitted.from ? previous.value : value),
  });
  protected readonly toText = linkedSignal<string, string>({
    source: this.to,
    computation: (value, previous) => (previous !== undefined && value === this.emitted.to ? previous.value : value),
  });

  protected readonly sessions = computed(() => (this.sessionsOnly() ? this.calendar.sessions() : null));
  protected readonly calendarProblem = computed(() => (this.sessionsOnly() ? this.calendar.problem() : null));
  protected readonly checked = computed(() => checkRange(this.fromText(), this.toText(), { bounds: this.bounds(), sessions: this.sessions() }));
  protected readonly problem = computed(() => this.checked().from.problem ?? this.checked().to.problem);
  protected readonly offers = computed(() => offerQuickRanges(this.ranges(), this.bounds(), this.calendar.sessions(), this.calendar.nowMs()));
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
    const market = sessions?.lastCompleted(this.calendar.nowMs()) ?? null;
    if (max !== null && maxReason !== null && market !== null && max < market) parts.push(maxReason);
    return parts.length === 0 ? null : parts.join(' · ');
  });

  /** Whether the fields have held dates yet; the fallback fills them once, never again after a host or a person has. */
  private seeded = false;

  constructor() {
    effect(() => {
      const key = this.fallback();
      if (this.fromText() !== '' || this.toText() !== '') this.seeded = true;
      if (this.seeded || key === null) return;
      const [offer] = offerQuickRanges([key], this.bounds(), this.calendar.sessions(), this.calendar.nowMs());
      if (offer.range === null) return;
      const { range } = offer;
      untracked(() => this.setRange(range));
    });
    effect(() => {
      const { from, to } = this.checked();
      untracked(() => {
        this.emitted = { from: from.date, to: to.date };
        if (this.from() !== from.date) this.from.set(from.date);
        if (this.to() !== to.date) this.to.set(to.date);
      });
    });
  }

  protected setRange(range: DateRange): void {
    this.fromText.set(range.from);
    this.toText.set(range.to);
  }

  protected applyOffer(offer: QuickRangeOffer): void {
    if (offer.range !== null) this.setRange(offer.range);
  }

  protected retryCalendar(): void {
    this.calendar.retry();
  }
}
