import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { isVaried, numberProblemKey, wireProtocol, type PlanEdit, type ProtocolFlag, type ProtocolNumberField } from './golden-search-plan-draft';
import { numberInputId, PAIR_AUDITS_INPUT_ID } from './golden-search-plan-problems';
import type { KnobPair, ProtocolRequest, StrategyCapability } from './golden-search.types';

interface PairOption {
  readonly pair: KnobPair;
  readonly label: string;
  readonly included: boolean;
  /** The held knob that keeps this pair off, by label; null while both knobs vary. */
  readonly heldBy: string | null;
}

/**
 * The plan's advanced controls (#2696), folded away by default: Zoom's
 * points, rounds and passes, the pair and neighbor audits, the recent-window
 * candidate, the final test's trade floor and the backtest cap the whole plan
 * must fit. A pair audit needs both its knobs to vary; while one is held the
 * pair stays chosen but is not sent, and it returns when the knob varies again.
 */
@Component({
  selector: 'app-golden-search-refinement-controls',
  imports: [DecimalPipe, InputText],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-refinement-controls.component.html',
  styleUrl: './golden-search-plan-card.scss',
})
export class GoldenSearchRefinementControlsComponent {
  readonly protocol = input.required<ProtocolRequest>();
  readonly capability = input.required<StrategyCapability | null>();
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly edit = output<PlanEdit>();

  protected readonly inputId = numberInputId;
  protected readonly pairsId = PAIR_AUDITS_INPUT_ID;

  protected readonly pairOptions = computed<PairOption[]>(() => {
    const labels = new Map((this.capability()?.knobs ?? []).map((knob) => [knob.name, knob.label]));
    const knobs = new Map(this.protocol().knobs.map((knob) => [knob.name, knob]));
    const chosen = this.protocol().pair_audits;
    const offered: KnobPair[] = [...(this.capability()?.default_pair_audits ?? [])];
    for (const pair of chosen) if (!offered.some((p) => p[0] === pair[0] && p[1] === pair[1])) offered.push(pair);
    return offered.map((pair) => {
      const held = pair.find((name) => { const knob = knobs.get(name); return knob === undefined || !isVaried(knob); });
      return {
        pair,
        label: `${labels.get(pair[0]) ?? pair[0]} × ${labels.get(pair[1]) ?? pair[1]}`,
        included: chosen.some((p) => p[0] === pair[0] && p[1] === pair[1]),
        heldBy: held === undefined ? null : (labels.get(held) ?? held),
      };
    });
  });

  /** The pair audits the plan sends, for the folded card's one-line summary. */
  protected readonly pairsSent = computed(() => wireProtocol(this.protocol()).pair_audits.length);

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
