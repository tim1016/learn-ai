import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { FILL_MODE_OPTIONS, isFillModeName } from '../../models/fill-mode';
import { etIsoDate } from '../../shared/date/et-midnight';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { RANKING_MEASURES } from '../grid-search/grid-search.types';
import { percentInputValue } from './golden-search-display';
import { dateProblemKey, numberProblemKey, type PlanEdit, type ProtocolDateField, type ProtocolFlag, type ProtocolNumberField } from './golden-search-plan-draft';
import type { KnobPair, ProtocolRequest, RankingMeasure, StrategyCapability } from './golden-search.types';

const NUMBER_LABELS: readonly (readonly [ProtocolNumberField, string])[] = [
  ['min_trades', 'Minimum trades per training winner'],
  ['drawdown_percent', 'Drawdown ceiling'],
  ['commission_per_order', 'Commission per order'],
  ['slippage_per_share', 'Slippage per share'],
  ['initial_cash', 'Starting capital'],
  ['training_months', 'Training months per fold'],
  ['test_months', 'Test months per fold'],
  ['zoom_points', 'Points per round'],
  ['zoom_refinements', 'Refinements per knob'],
  ['zoom_passes', 'Passes over the knobs'],
  ['exam_min_trades', 'Minimum final-test trades'],
  ['budget_cap', 'Backtest cap'],
];

const DATE_LABELS: readonly (readonly [ProtocolDateField, string])[] = [
  ['development_start', 'Development from'],
  ['final_start', 'Final test from'],
  ['final_end', 'Final test through'],
];

/** Problem key → the label of the control it belongs to, in panel order. */
const FIELD_LABELS: readonly { key: string; label: string }[] = [
  ...NUMBER_LABELS.map(([field, label]) => ({ key: numberProblemKey(field), label })),
  ...DATE_LABELS.map(([field, label]) => ({ key: dateProblemKey(field), label })),
];

interface PairOption {
  readonly pair: KnobPair;
  readonly label: string;
  readonly included: boolean;
}

/**
 * The protocol controls frozen at lock (#2696): selection rules, execution
 * assumptions, the development and final-test intervals (ET dates), fold
 * lengths, Zoom settings, audits and the workload cap. Every value is a
 * starting policy the trader may change before lock; the server preflight
 * judges the result.
 */
@Component({
  selector: 'app-golden-search-protocol-controls',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-protocol-controls.component.html',
  styleUrl: './golden-search-protocol-controls.component.scss',
})
export class GoldenSearchProtocolControlsComponent {
  readonly protocol = input.required<ProtocolRequest>();
  readonly capability = input.required<StrategyCapability | null>();
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly edit = output<PlanEdit>();

  protected readonly measures = RANKING_MEASURES;
  protected readonly fillModes = FILL_MODE_OPTIONS;
  protected readonly drawdownPercent = computed(() => percentInputValue(this.protocol().policy.max_drawdown_ceiling));
  protected readonly dates = computed(() => {
    const p = this.protocol();
    return { development_start: etIsoDate(p.development_start_ms), final_start: etIsoDate(p.final_start_ms), final_end: etIsoDate(p.final_end_ms - 1) };
  });
  protected readonly pairOptions = computed<PairOption[]>(() => {
    const labels = new Map((this.capability()?.knobs ?? []).map((knob) => [knob.name, knob.label]));
    const chosen = this.protocol().pair_audits;
    const offered = [...(this.capability()?.default_pair_audits ?? [])];
    for (const pair of chosen) if (!offered.some((p) => p[0] === pair[0] && p[1] === pair[1])) offered.push(pair);
    return offered.map((pair) => ({
      pair,
      label: `${labels.get(pair[0]) ?? pair[0]} × ${labels.get(pair[1]) ?? pair[1]}`,
      included: chosen.some((p) => p[0] === pair[0] && p[1] === pair[1]),
    }));
  });

  /** This panel's unreadable inputs, named; knob inputs report theirs in the knob table. */
  protected readonly fieldProblems = computed(() => {
    const problems = this.problems();
    return FIELD_LABELS.flatMap(({ key, label }) => {
      const message = problems.get(key);
      return message === undefined ? [] : [{ key, label, message }];
    });
  });

  protected numberInvalid(field: ProtocolNumberField): boolean {
    return this.problems().has(numberProblemKey(field));
  }

  protected dateInvalid(field: ProtocolDateField): boolean {
    return this.problems().has(dateProblemKey(field));
  }

  protected onNumber(field: ProtocolNumberField, raw: string): void {
    this.edit.emit({ kind: 'number', field, raw });
  }

  protected onDate(field: ProtocolDateField, raw: string): void {
    this.edit.emit({ kind: 'date', field, raw });
  }

  protected onFlag(field: ProtocolFlag, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.edit.emit({ kind: 'flag', field, value: event.target.checked });
  }

  protected onPair(pair: KnobPair, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.edit.emit({ kind: 'pair', pair, included: event.target.checked });
  }

  protected onObjective(event: Event): void {
    const raw = event.target instanceof HTMLSelectElement ? event.target.value : '';
    const objective = this.measures.find((measure): measure is RankingMeasure => measure === raw);
    if (objective !== undefined) this.edit.emit({ kind: 'objective', objective });
  }

  protected onFillMode(event: Event): void {
    const raw = event.target instanceof HTMLSelectElement ? event.target.value : null;
    if (isFillModeName(raw)) this.edit.emit({ kind: 'fill-mode', fillMode: raw });
  }
}
