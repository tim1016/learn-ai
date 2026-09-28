import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';
import type {
  BotPanelView,
  ChannelHealthView,
  CurrentRunState,
  FeedContinuityView,
  PanelActionTrigger,
  PanelProfile,
} from '../lib/broker-v2-panel.types';
import { EMPTY_CURRENT_RUN_STATE, feedContinuityFor } from '../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { OperatorReadinessComponent } from '../operator-lens/operator-readiness.component';
import { HealthCardComponent } from '../operator-lens/health-card.component';
import { OperatorRunHistoryComponent } from '../bot-run-history/operator-run-history.component';
import { BotConnectionsComponent } from './bot-connections.component';
import { BotOrderRecordsComponent } from './bot-order-records.component';

/** "all connected" / "1 needs attention" / "2 need attention", from channel state alone. */
function connectionsStatus(channels: readonly ChannelHealthView[]): string {
  if (channels.length === 0) return 'none reported';
  const attention = channels.filter((channel) => channel.state !== 'healthy').length;
  if (attention === 0) return 'all connected';
  return attention === 1 ? '1 needs attention' : `${attention} need attention`;
}

function workingOrdersStatus(count: number): string {
  if (count === 0) return 'no working orders';
  return count === 1 ? '1 working order' : `${count} working orders`;
}

/**
 * The bot page's Details (#2563, PRD #2560 D2 and story 47): exit terms,
 * checks, connections, order records and run evidence, each in a native
 * `<details>` fold that starts closed.
 *
 * Folds render their bodies eagerly except where a body would read the
 * server: the audit trail loads only once Order records is first opened, and
 * previous runs load only when the owner asks for them inside Run evidence.
 */
@Component({
  selector: 'app-bot-details',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BotConnectionsComponent,
    BotOrderRecordsComponent,
    HealthCardComponent,
    OperatorReadinessComponent,
    OperatorRunHistoryComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: './bot-details.component.html',
  styleUrl: './bot-details.component.scss',
})
export class BotDetailsComponent {
  readonly panel = input.required<BotPanelView>();
  readonly profile = input.required<PanelProfile>();
  readonly actionPending = input(false);
  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();
  readonly currentRunState = input<CurrentRunState>(EMPTY_CURRENT_RUN_STATE);

  readonly actionRequested = output<PanelActionTrigger>();
  readonly transactionSelected = output<string>();
  readonly runRefreshRequested = output();

  /** Order records reads the audit trail from its first opening onwards. */
  protected readonly orderRecordsActivated = signal(false);

  protected readonly exitTerms = computed(() => this.panel().exit_terms ?? null);
  protected readonly feedContinuity = computed<FeedContinuityView>(() =>
    feedContinuityFor(this.panel()),
  );
  protected readonly connectionsStatus = computed(() =>
    connectionsStatus(this.panel().clerk.channels),
  );
  protected readonly workingOrdersStatus = computed(() =>
    workingOrdersStatus(this.panel().working_orders.length),
  );

  protected onOrderRecordsToggle(open: boolean): void {
    if (open) this.orderRecordsActivated.set(true);
  }
}
