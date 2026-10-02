import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { fullPointEntries } from './golden-search-display';
import { knobProblemKey, type KnobNumberField, type PlanEdit } from './golden-search-plan-draft';
import type { CapabilityKnob, GoldenSearchMethod, KnobPlan, Point, PointValue, StrategyCapability } from './golden-search.types';

const VALUE_FIELDS: readonly { field: KnobNumberField; label: string }[] = [
  { field: 'low', label: 'Low' },
  { field: 'high', label: 'High' },
  { field: 'fixed_value', label: 'Fixed value' },
];

interface KnobRow {
  readonly plan: KnobPlan;
  readonly knob: CapabilityKnob | null;
  readonly label: string;
  readonly first: boolean;
  readonly last: boolean;
  /** The starting value of a searched knob when it lies outside the searched range, else null. */
  readonly startOutside: number | null;
}

function outsideRange(plan: KnobPlan, start: PointValue | undefined): number | null {
  return plan.mode === 'search' && typeof start === 'number' && (start < plan.low || start > plan.high) ? start : null;
}

/**
 * The knob table (#2696): every declared knob in search order with its unit
 * and legal domain, Search or Keep fixed, the searched range and its smallest
 * step or the fixed value, and up/down to change the search order. With the
 * plan's starting point, a searched knob that starts outside its range says
 * so (the server accepts it). Fixed controls and constraints are listed
 * read-only beneath. Read-only for a locked study's frozen plan.
 */
@Component({
  selector: 'app-golden-search-knob-table',
  imports: [InputText],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-knob-table.component.html',
  styleUrl: './golden-search-knob-table.component.scss',
})
export class GoldenSearchKnobTableComponent {
  readonly knobs = input.required<readonly KnobPlan[]>();
  readonly capability = input.required<StrategyCapability | null>();
  readonly method = input.required<GoldenSearchMethod>();
  /** The plan's starting point (the seed, or the incumbent's params); a knob it omits starts at its declared default. */
  readonly start = input<Point | null>(null);
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly readonly = input(false);
  readonly edit = output<PlanEdit>();

  protected readonly rows = computed<KnobRow[]>(() => {
    const declared = new Map((this.capability()?.knobs ?? []).map((knob) => [knob.name, knob]));
    const start = this.start();
    const starting = new Map(start === null ? [] : fullPointEntries(start, this.capability()).map((entry) => [entry.name, entry.value]));
    const plans = this.knobs();
    return plans.map((plan, index) => {
      const knob = declared.get(plan.name) ?? null;
      return { plan, knob, label: knob?.label ?? plan.name, first: index === 0, last: index === plans.length - 1, startOutside: outsideRange(plan, starting.get(plan.name)) };
    });
  });
  protected readonly stepHint = computed(() =>
    this.method() === 'grid'
      ? 'Grid tests every value from low to high at the smallest step.'
      : 'Zoom stops narrowing a knob once its spacing reaches the smallest step.',
  );

  protected problem(name: string, field: KnobNumberField): string | null {
    return this.problems().get(knobProblemKey(name, field)) ?? null;
  }

  /** The unreadable inputs of one row's value cell (low, high, fixed value), each named. */
  protected valueProblems(name: string): { field: KnobNumberField; label: string; message: string }[] {
    return VALUE_FIELDS.flatMap(({ field, label }) => {
      const message = this.problem(name, field);
      return message === null ? [] : [{ field, label, message }];
    });
  }

  protected onMode(name: string, event: Event): void {
    const value = event.target instanceof HTMLSelectElement ? event.target.value : null;
    if (value === 'search' || value === 'fixed') this.edit.emit({ kind: 'knob-mode', name, mode: value });
  }

  protected onNumber(name: string, field: KnobNumberField, raw: string): void {
    this.edit.emit({ kind: 'knob-number', name, field, raw });
  }

  protected move(name: string, offset: -1 | 1): void {
    this.edit.emit({ kind: 'knob-move', name, offset });
  }
}
