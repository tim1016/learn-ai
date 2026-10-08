import { DOCUMENT } from '@angular/common';
import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, inject, input, output, viewChild } from '@angular/core';

import { DateFieldComponent } from '../../../../shared/date-picker';
import { etIsoDate } from '../../../../shared/date/et-midnight';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { onExpiryRule, onStrikeRule, problemLines, rowProblems, type LegRow, type OptionRules, type RowBox } from './option-structure';

let nextDateId = 0;

/** What a select was set to, or `null` for an event that is not a select's. */
function chosen(event: Event): string | null {
  return event.target instanceof HTMLSelectElement ? event.target.value : null;
}

/** The number a box holds, or `null` while it holds none. */
function typed(event: Event): number | null {
  if (!(event.target instanceof HTMLInputElement)) return null;
  const text = event.target.value.trim();
  const value = Number(text);
  return text !== '' && Number.isFinite(value) ? value : null;
}

/**
 * One leg of the structure builder (#2915), as a row of its table: side and
 * type as words, the quantity, the strike rule with its number and its
 * "Within" in the rule's own unit, and the expiry rule. A rule with no
 * tolerance has no "Within" box. The rules, their labels, units and starting
 * tolerances are the catalogue's.
 *
 * A number is taken when its box is left or Enter is pressed, so a
 * half-typed one is never read. A box that is empty, or holds what its rule
 * cannot take, leaves the row unfinished: the builder neither previews nor
 * uses it, and the row says under its boxes what each one still needs.
 */
@Component({
  selector: 'app-option-leg-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DateFieldComponent, ReceiptLabelPipe],
  templateUrl: './option-leg-row.component.html',
  styleUrl: './option-leg-row.component.scss',
  host: { role: 'row' },
})
export class OptionLegRowComponent {
  readonly row = input.required<LegRow>();
  /** The row's place in the table, from 1: how the screen names the leg. */
  readonly number = input.required<number>();
  /** Every row of the table, this one among them: what a distance can be measured from. */
  readonly rows = input.required<readonly LegRow[]>();
  readonly rules = input.required<OptionRules>();

  readonly rowChange = output<LegRow>();
  readonly remove = output();

  private readonly page = inject(DOCUMENT);
  private readonly dateField = viewChild(DateFieldComponent);
  private readonly dateElement = viewChild(DateFieldComponent, { read: ElementRef<HTMLElement> });

  private readonly id = nextDateId++;
  protected readonly dateId = `option-leg-date-${this.id}`;
  protected readonly problemsId = `option-leg-problems-${this.id}`;
  /**
   * Today in New York, where an expiry date lives: the first day the calendar
   * offers. A date that has passed can still be typed, and the server says why
   * it refuses one.
   */
  protected readonly today = etIsoDate(Date.now());
  protected readonly name = computed(() => `Leg ${this.number()}`);
  protected readonly strikeRule = computed(
    () => this.rules().strike_rules.find((rule) => rule.selector === this.row().strike.selector) ?? null,
  );
  protected readonly expiryRule = computed(
    () => this.rules().expiry_rules.find((rule) => rule.selector === this.row().expiry.selector) ?? null,
  );
  /** What each box still needs before this row is a leg, in words: a red outline is never the only sign. */
  protected readonly problems = computed(() => rowProblems(this.row(), this.rows(), this.rules()));
  /** A fixed date's problem is the date field's to say, under its own box. */
  protected readonly dateProblem = computed(() => (this.row().expiry.selector === 'absolute' ? this.problems().expiry ?? null : null));
  protected readonly problemLines = computed(() =>
    problemLines(this.dateProblem() === null ? this.problems() : { ...this.problems(), expiry: undefined }));
  /** A box's own line under the row, for the box to point at: each invalid box is tied to its words alone. */
  protected lineId(box: RowBox): string {
    return `${this.problemsId}-${box}`;
  }
  /** The other legs, by their numbers. */
  protected readonly others = computed(() =>
    this.rows()
      .map((row, index) => ({ leg_id: row.leg_id, number: index + 1 }))
      .filter((other) => other.leg_id !== this.row().leg_id),
  );

  constructor() {
    // The drawer around the builder closes on any Escape that reaches the
    // page, wherever the keyboard is. While this leg's calendar is open the
    // key is the calendar's: it is taken on its way down, before anything
    // else hears it, so the builder stays open with its edits. With no
    // calendar open nothing listens here, and the key is the builder's.
    effect((onCleanup) => {
      const field = this.dateField();
      if (field === undefined || !field.calendarOpen()) return;
      const takeEscape = (event: KeyboardEvent): void => {
        if (event.key !== 'Escape') return;
        event.stopPropagation();
        const keyboardInField = this.dateElement()?.nativeElement.contains(this.page.activeElement) ?? false;
        field.closeCalendar();
        // The calendar goes, and the keyboard with it if it was on a day: it stays with the field.
        if (keyboardInField) field.focus();
      };
      this.page.addEventListener('keydown', takeEscape, true);
      onCleanup(() => this.page.removeEventListener('keydown', takeEscape, true));
    });
  }

  protected setPosition(event: Event): void {
    const position = chosen(event);
    if (position === 'long' || position === 'short') this.rowChange.emit({ ...this.row(), position });
  }

  protected setRight(event: Event): void {
    const right = chosen(event);
    if (right === 'call' || right === 'put') this.rowChange.emit({ ...this.row(), right });
  }

  protected setQuantity(event: Event): void {
    this.rowChange.emit({ ...this.row(), qty_ratio: typed(event) });
  }

  protected setStrikeRule(event: Event): void {
    const selector = chosen(event);
    const rule = this.rules().strike_rules.find((offered) => offered.selector === selector);
    if (rule) this.rowChange.emit(onStrikeRule(this.row(), rule, this.rows()));
  }

  protected setStrikeValue(event: Event): void {
    this.setStrike({ value: typed(event) });
  }

  protected setTolerance(event: Event): void {
    this.setStrike({ tolerance: typed(event) });
  }

  protected setFromLeg(event: Event): void {
    const legId = chosen(event);
    if (this.others().some((other) => other.leg_id === legId)) this.setStrike({ from_leg: legId });
  }

  protected setExpiryRule(event: Event): void {
    const selector = chosen(event);
    const rule = this.rules().expiry_rules.find((offered) => offered.selector === selector);
    if (rule) this.rowChange.emit(onExpiryRule(this.row(), rule));
  }

  protected setDays(event: Event): void {
    this.rowChange.emit({ ...this.row(), expiry: { ...this.row().expiry, days: typed(event) } });
  }

  protected setDate(date: string): void {
    if (date !== this.row().expiry.date) this.rowChange.emit({ ...this.row(), expiry: { ...this.row().expiry, date } });
  }


  private setStrike(change: Partial<LegRow['strike']>): void {
    this.rowChange.emit({ ...this.row(), strike: { ...this.row().strike, ...change } });
  }
}
