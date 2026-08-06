import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { SqliteSafeFlattenPlan } from '../../../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../../shared/timestamp';

/** Pure presentation for one backend-authored, read-only reduction plan. */
@Component({
  selector: 'app-safe-flatten-plan',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './safe-flatten-plan.component.html',
  styleUrl: './safe-flatten-plan.component.scss',
})
export class SafeFlattenPlanComponent {
  readonly plan = input.required<SqliteSafeFlattenPlan>();

  protected trackLeg(
    _index: number,
    leg: SqliteSafeFlattenPlan['legs'][number],
  ): string {
    return `${leg.strategy_instance_id}:${leg.symbol}`;
  }
}
