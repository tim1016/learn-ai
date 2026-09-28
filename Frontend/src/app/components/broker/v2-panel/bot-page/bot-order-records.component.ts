import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  resource,
} from '@angular/core';
import type { BotPanelView, PanelProfile } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { resourceTarget } from '../../../../fleet/resource-target';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { TransactionRailComponent } from '../operator-lens/transaction-rail.component';
import { JournalTailComponent } from '../operator-lens/journal-tail.component';

/**
 * The bot page's Order records fold body (#2563): the bot's working orders,
 * the selected order's recorded path, and the audit trail.
 *
 * The audit trail is read from the server only once `activated` turns true
 * (the host sets it when the fold is first opened), then reloads only for a
 * new journal cursor or lane provenance — panel polling alone is inert.
 */
@Component({
  selector: 'app-bot-order-records',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    JournalTailComponent,
    ReceiptLabelPipe,
    TransactionRailComponent,
  ],
  templateUrl: './bot-order-records.component.html',
  styleUrl: './bot-order-records.component.scss',
})
export class BotOrderRecordsComponent {
  readonly panel = input.required<BotPanelView>();
  readonly profile = input.required<PanelProfile>();
  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();
  /** True once the owner has opened the fold; never falls back to false. */
  readonly activated = input(false);

  readonly transactionSelected = output<string>();

  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);

  private readonly target = computed(() => {
    const lane = this.fleetDirectory.lane(this.broker(), this.clerkId());
    return resourceTarget(this.broker(), this.clerkId(), {
      accountId: this.accountId(),
      entityId: this.sid(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
  });

  protected readonly journalPage = resource({
    params: () => this.activated()
      ? [
          this.broker(),
          this.clerkId(),
          this.accountId(),
          this.sid(),
          this.target().bindingGeneration ?? '',
          this.target().routingEpoch ?? '',
          this.panel().journal_tail_seq ?? 'empty',
        ].join('|')
      : undefined,
    loader: () =>
      this.panelSvc.getEvidence(this.target(), this.sid(), {
        pageSize: 24,
        clientHint: 'bot-page-order-records',
      }),
  });
}
