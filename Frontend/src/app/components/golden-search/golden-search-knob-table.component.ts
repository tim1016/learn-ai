import { afterNextRender, ChangeDetectionStrategy, Component, computed, ElementRef, inject, Injector, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { fullPointEntries } from './golden-search-display';
import { isRanked, isVaried, knobProblemKey, type KnobNumberField, type PlanEdit } from './golden-search-plan-draft';
import { importanceInputId, knobInputId, refusalKnob, withoutLabel } from './golden-search-plan-problems';
import type { CapabilityKnob, GoldenSearchMethod, KnobPlan, Point, PointValue, ProtocolRefusal, StrategyCapability } from './golden-search.types';

const VALUE_FIELDS: readonly { field: KnobNumberField; label: string }[] = [
  { field: 'low', label: 'Low' },
  { field: 'high', label: 'High' },
  { field: 'fixed_value', label: 'Held value' },
];

interface KnobRow {
  readonly plan: KnobPlan;
  readonly knob: CapabilityKnob | null;
  readonly label: string;
  readonly searched: boolean;
  /** Searched from a value to itself: the plan holds the knob there. */
  readonly single: boolean;
  readonly golden: PointValue | null;
  /** Where the search starts, shown only when it is not the golden value. */
  readonly startsAt: PointValue | null;
  /** The starting value of a searched knob when it lies outside the searched range, else null. */
  readonly startOutside: number | null;
  /** The server's count of the knob's settings; undefined until the plan is checked, null when its range is not valid. */
  readonly values: number | null | undefined;
  readonly rangeRefusals: readonly string[];
  readonly stepRefusals: readonly string[];
  readonly importanceRefusals: readonly string[];
}

function outsideRange(plan: KnobPlan, start: PointValue | undefined): number | null {
  return isVaried(plan) && typeof start === 'number' && (start < plan.low || start > plan.high) ? start : null;
}

/**
 * The knob table (#2696): every declared knob in search order with its unit,
 * legal domain and golden value. Vary searches a knob from low to high at its
 * smallest step; off, it is held at a value (its starting value unless edited).
 * A range whose ends meet is held at that value too. The Values column is the
 * server's count of each knob's settings, and the server's refusals for a knob
 * show on its row. Each knob's importance (ADR 0074 decision 10) sets the search
 * order, most important first and ties in the strategy's order; a legacy plan
 * without importance keeps its own order. Fixed program controls and the
 * declared rules sit beneath. Read-only for a locked study's frozen plan.
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
  /** The plan's seed; a knob it omits starts at its declared default. */
  readonly start = input<Point | null>(null);
  /** The frozen incumbent's params: the golden values the search is judged against. */
  readonly golden = input<Point | null>(null);
  /** The server's count of each knob's settings, by knob name; null until the plan is checked. */
  readonly values = input<ReadonlyMap<string, number | null> | null>(null);
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly refusals = input<readonly ProtocolRefusal[]>([]);
  readonly readonly = input(false);
  readonly edit = output<PlanEdit>();

  protected readonly inputId = knobInputId;
  protected readonly importanceId = importanceInputId;
  private readonly injector = inject(Injector);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /** Whether the plan ranks its knobs by importance; a legacy plan has no importance column. */
  protected readonly ranked = computed(() => isRanked(this.knobs()));
  protected readonly scale = computed(() => {
    const scale = this.capability()?.importance;
    return scale === undefined ? [] : Array.from({ length: scale.high - scale.low + 1 }, (_, index) => scale.low + index);
  });

  private readonly pointValues = (point: Point | null): ReadonlyMap<string, PointValue> =>
    new Map(point === null ? [] : fullPointEntries(point, this.capability()).map((entry) => [entry.name, entry.value]));

  protected readonly rows = computed<KnobRow[]>(() => {
    const declared = new Map((this.capability()?.knobs ?? []).map((knob) => [knob.name, knob]));
    const starting = this.pointValues(this.start());
    const golden = this.pointValues(this.golden());
    const values = this.values();
    const refused = this.refusalsByKnob();
    return this.knobs().map((plan) => {
      const knob = declared.get(plan.name) ?? null;
      const label = knob?.label ?? plan.name;
      const goldenValue = golden.get(plan.name) ?? null;
      const startValue = starting.get(plan.name);
      return {
        plan,
        knob,
        label,
        searched: plan.mode === 'search',
        single: plan.mode === 'search' && !isVaried(plan),
        golden: goldenValue,
        startsAt: startValue !== undefined && goldenValue !== null && startValue !== goldenValue ? startValue : null,
        startOutside: outsideRange(plan, startValue),
        values: values === null ? undefined : (values.get(plan.name) ?? null),
        rangeRefusals: (refused.get(plan.name)?.value ?? []).map((message) => withoutLabel(message, label)),
        stepRefusals: (refused.get(plan.name)?.step ?? []).map((message) => withoutLabel(message, label)),
        importanceRefusals: (refused.get(plan.name)?.importance ?? []).map((message) => withoutLabel(message, label)),
      };
    });
  });

  protected readonly stepHint = computed(() =>
    this.method() === 'grid' ? 'Grid tries every value from low to high at the step.' : 'Zoom stops narrowing a knob once its spacing reaches the step.',
  );
  protected readonly hasWarmup = computed(() => (this.capability()?.knobs ?? []).some((knob) => knob.warmup_dependent));
  protected readonly rules = computed(() => {
    const labels = new Map((this.capability()?.knobs ?? []).map((knob) => [knob.name, knob.label]));
    return (this.capability()?.constraints ?? []).map((constraint) => ({
      key: `${constraint.left}${constraint.op}${constraint.right}`,
      text: `${labels.get(constraint.left) ?? constraint.left} ${constraint.op} ${labels.get(constraint.right) ?? constraint.right}`,
      message: constraint.message,
    }));
  });

  private readonly refusalsByKnob = computed(() => {
    const byKnob = new Map<string, { value: string[]; step: string[]; importance: string[] }>();
    for (const refusal of this.refusals()) {
      const named = refusalKnob(refusal.field);
      if (named === null) continue;
      const entry = byKnob.get(named.name) ?? { value: [], step: [], importance: [] };
      entry[named.part].push(refusal.message);
      byKnob.set(named.name, entry);
    }
    return byKnob;
  });

  protected problem(name: string, field: KnobNumberField): string | null {
    return this.problems().get(knobProblemKey(name, field)) ?? null;
  }

  /** The unreadable inputs of one row's value cell (low, high, held value), each named. */
  protected valueProblems(name: string): { field: KnobNumberField; label: string; message: string }[] {
    return VALUE_FIELDS.flatMap(({ field, label }) => {
      const message = this.problem(name, field);
      return message === null ? [] : [{ field, label, message }];
    });
  }

  protected onVary(name: string, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.edit.emit({ kind: 'knob-mode', name, mode: event.target.checked ? 'search' : 'fixed' });
  }

  protected onNumber(name: string, field: KnobNumberField, raw: string): void {
    this.edit.emit({ kind: 'knob-number', name, field, raw });
  }

  /** A new score can move the row; focus stays on its importance control so the keyboard does not lose its place. */
  protected onImportance(name: string, event: Event): void {
    if (!(event.target instanceof HTMLSelectElement)) return;
    this.edit.emit({ kind: 'knob-importance', name, importance: Number(event.target.value) });
    const id = importanceInputId(name);
    afterNextRender(() => this.host.nativeElement.querySelector<HTMLSelectElement>(`[id="${id}"]`)?.focus(), { injector: this.injector });
  }
}
