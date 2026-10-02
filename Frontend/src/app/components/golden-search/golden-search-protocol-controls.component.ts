import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { FILL_MODE_OPTIONS, isFillModeName } from '../../models/fill-mode';
import { etIsoDate } from '../../shared/date/et-midnight';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { RANKING_MEASURES } from '../grid-search/grid-search.types';
import { percentInputValue } from './golden-search-display';
import { dateProblemKey, numberProblemKey, type PlanEdit, type ProtocolDateField, type ProtocolFlag, type ProtocolNumberField } from './golden-search-plan-draft';
import type { ProtocolRequest, RankingMeasure, StrategyCapability } from './golden-search.types';

const NUMBER_LABELS: readonly (readonly [ProtocolNumberField, string])[] = [
  ['min_trades', 'Minimum completed trades'],
  ['drawdown_percent', 'Maximum drawdown'],
  ['commission_per_order', 'Commission per order'],
  ['slippage_per_share', 'Slippage per share'],
  ['initial_cash', 'Starting capital'],
  ['training_months', 'Training window (months)'],
  ['test_months', 'Test window (months)'],
  ['final_months', 'Final test (months)'],
  ['zoom_points', 'Points per round'],
  ['zoom_refinements', 'Refinement rounds'],
  ['zoom_passes', 'Maximum passes'],
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

/**
 * What would make the trader reject a candidate (#2696), frozen at lock: the
 * objective, drawdown ceiling and trade floor, the fold and final-test
 * lengths in months (the server lays the dates out from them), and — on
 * request — capital, fills and costs, and the dates themselves. Every value
 * is a starting policy; the server preflight judges the plan. The list of
 * unreadable values below covers every protocol field, wherever it sits.
 */
@Component({
  selector: 'app-golden-search-protocol-controls',
  imports: [InputText, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-protocol-controls.component.html',
  styleUrl: './golden-search-protocol-controls.component.scss',
})
export class GoldenSearchProtocolControlsComponent {
  readonly protocol = input.required<ProtocolRequest>();
  readonly capability = input.required<StrategyCapability | null>();
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  /** The final test's length in months; undefined while the dates do not follow a month count. */
  readonly finalMonths = input<number | undefined>(undefined);
  readonly edit = output<PlanEdit>();

  protected readonly measures = RANKING_MEASURES;
  protected readonly fillModes = FILL_MODE_OPTIONS;
  protected readonly drawdownPercent = computed(() => percentInputValue(this.protocol().policy.max_drawdown_ceiling));
  protected readonly dates = computed(() => {
    const p = this.protocol();
    return { development_start: etIsoDate(p.development_start_ms), final_start: etIsoDate(p.final_start_ms), final_end: etIsoDate(p.final_end_ms - 1) };
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
