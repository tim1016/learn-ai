import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { DateFieldComponent } from '../../../../shared/date-picker';
import { etIsoDate } from '../../../../shared/date/et-midnight';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { onExpiryRule, onStrikeRule, type LegRow, type OptionRules } from './option-structure';

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
 * half-typed one is never read. An empty box is an unfinished row, which the
 * builder neither previews nor uses.
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

  protected readonly dateId = `option-leg-date-${nextDateId++}`;
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
  /** The other legs, by their numbers. */
  protected readonly others = computed(() =>
    this.rows()
      .map((row, index) => ({ leg_id: row.leg_id, number: index + 1 }))
      .filter((other) => other.leg_id !== this.row().leg_id),
  );

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

  /**
   * Escape with this leg's calendar open puts the calendar away and goes no
   * further, so the builder around it stays open with its edits. With no
   * calendar open the key is the builder's. The field's own button says
   * whether its calendar is open.
   */
  protected keepEscape(event: Event, field: DateFieldComponent): void {
    const within = event.currentTarget;
    if (!(within instanceof HTMLElement) || within.querySelector('button[aria-expanded="true"]') === null) return;
    event.stopPropagation();
    field.closeCalendar();
  }

  private setStrike(change: Partial<LegRow['strike']>): void {
    this.rowChange.emit({ ...this.row(), strike: { ...this.row().strike, ...change } });
  }
}
