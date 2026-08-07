import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { CardModule } from 'primeng/card';
import { MessageModule } from 'primeng/message';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { timer } from 'rxjs';

import type { SqliteSafeFlattenPlan } from '../../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';

/** Pure presentation for one backend-authored, read-only reduction plan. */
@Component({
  selector: 'app-safe-flatten-plan',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CardModule,
    MessageModule,
    ReceiptLabelPipe,
    TableModule,
    TagModule,
    TimestampDisplayComponent,
  ],
  templateUrl: './safe-flatten-plan.component.html',
})
export class SafeFlattenPlanComponent {
  readonly plan = input.required<SqliteSafeFlattenPlan>();
  private readonly displayClock = toSignal(timer(0, 1_000), { initialValue: 0 });
  protected readonly expired = computed(() => {
    this.displayClock();
    return Date.now() >= this.plan().expires_at_ms;
  });

  protected trackLeg(
    _index: number,
    leg: SqliteSafeFlattenPlan['legs'][number],
  ): string {
    return `${leg.strategy_instance_id}:${leg.symbol}`;
  }
}
