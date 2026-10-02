import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { RANKING_MEASURES } from '../grid-search/grid-search.types';
import { percentInputValue, usesTradeFrequency } from './golden-search-display';
import { GoldenSearchActivityComponent } from './golden-search-activity.component';
import { numberProblemKey, type PlanEdit, type ProtocolFlag, type ProtocolNumberField } from './golden-search-plan-draft';
import { numberInputId } from './golden-search-plan-problems';
import type { GoldenSearchPreflight, ProtocolRequest, RankingMeasure } from './golden-search.types';

/**
 * How to judge a candidate (#2696), frozen at lock: the objective that ranks
 * candidates, and the drawdown ceiling, trade floor (an expected trade
 * frequency, or a legacy plan's fixed floor) and positive-net rule that
 * reject one whatever its score. Every value is a starting policy; the
 * server preflight judges the plan.
 */
@Component({
  selector: 'app-golden-search-protocol-controls',
  imports: [ButtonModule, GoldenSearchActivityComponent, InputText, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-protocol-controls.component.html',
  styleUrl: './golden-search-plan-card.scss',
})
export class GoldenSearchProtocolControlsComponent {
  readonly protocol = input.required<ProtocolRequest>();
  readonly preflight = input<GoldenSearchPreflight | null>(null);
  /** The rate a legacy plan adopts when switched to an expected trade frequency (the server's default). */
  readonly defaultRate = input<number | null>(null);
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly edit = output<PlanEdit>();

  protected readonly measures = RANKING_MEASURES;
  protected readonly inputId = numberInputId;
  protected readonly drawdownPercent = computed(() => percentInputValue(this.protocol().policy.max_drawdown_ceiling));
  protected readonly frequencyBased = computed(() => usesTradeFrequency(this.protocol()));

  protected numberInvalid(field: ProtocolNumberField): boolean {
    return this.problems().has(numberProblemKey(field));
  }

  protected onNumber(field: ProtocolNumberField, raw: string): void {
    this.edit.emit({ kind: 'number', field, raw });
  }

  protected onFlag(field: ProtocolFlag, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.edit.emit({ kind: 'flag', field, value: event.target.checked });
  }

  protected onObjective(event: Event): void {
    const raw = event.target instanceof HTMLSelectElement ? event.target.value : '';
    const objective = this.measures.find((measure): measure is RankingMeasure => measure === raw);
    if (objective !== undefined) this.edit.emit({ kind: 'objective', objective });
  }
}
