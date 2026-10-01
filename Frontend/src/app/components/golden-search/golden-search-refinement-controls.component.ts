import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { numberProblemKey, type PlanEdit, type ProtocolFlag, type ProtocolNumberField } from './golden-search-plan-draft';
import type { KnobPair, ProtocolRequest, StrategyCapability } from './golden-search.types';

interface PairOption {
  readonly pair: KnobPair;
  readonly label: string;
  readonly included: boolean;
}

/**
 * Search order and refinement controls (#2696), on request under the knob
 * table: Zoom's points, rounds and passes, the pair and neighbor audits the
 * evidence needs, and the backtest cap the whole plan must fit. Pair audits
 * count toward the workload the server estimates.
 */
@Component({
  selector: 'app-golden-search-refinement-controls',
  imports: [InputText],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-refinement-controls.component.html',
  styleUrl: './golden-search-protocol-controls.component.scss',
})
export class GoldenSearchRefinementControlsComponent {
  readonly protocol = input.required<ProtocolRequest>();
  readonly capability = input.required<StrategyCapability | null>();
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly edit = output<PlanEdit>();

  protected readonly pairOptions = computed<PairOption[]>(() => {
    const labels = new Map((this.capability()?.knobs ?? []).map((knob) => [knob.name, knob.label]));
    const chosen = this.protocol().pair_audits;
    const offered: KnobPair[] = [...(this.capability()?.default_pair_audits ?? [])];
    for (const pair of chosen) if (!offered.some((p) => p[0] === pair[0] && p[1] === pair[1])) offered.push(pair);
    return offered.map((pair) => ({
      pair,
      label: `${labels.get(pair[0]) ?? pair[0]} × ${labels.get(pair[1]) ?? pair[1]}`,
      included: chosen.some((p) => p[0] === pair[0] && p[1] === pair[1]),
    }));
  });

  protected numberInvalid(field: ProtocolNumberField): boolean {
    return this.problems().has(numberProblemKey(field));
  }

  protected onNumber(field: ProtocolNumberField, raw: string): void {
    this.edit.emit({ kind: 'number', field, raw });
  }

  protected onFlag(field: ProtocolFlag, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.edit.emit({ kind: 'flag', field, value: event.target.checked });
  }

  protected onPair(pair: KnobPair, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.edit.emit({ kind: 'pair', pair, included: event.target.checked });
  }
}
