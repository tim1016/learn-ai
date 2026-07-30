import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { BrokerV2PanelService } from '../broker-v2-panel.service';
import type { BotCatalogView, PanelActionRequest } from '../models/broker-v2-panel.types';
import { AccountStripComponent } from '../account-strip/account-strip.component';
import { BotsRosterComponent, type RowActionEvent } from '../bots-roster/bots-roster.component';
import { DeployDialogComponent } from '../deploy-dialog/deploy-dialog.component';

/**
 * Bots list page — route target for
 *   `/brokers/:broker/accounts/:accountId/bots` (account-scoped)
 *   `/brokers/:broker/bots`                     (unscoped fallback)
 *
 * Polls the catalog every 5s and passes live data down to the roster.
 * The profile is loaded once (it's static per-broker metadata).
 */
@Component({
  selector: 'app-bots-list-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountStripComponent,
    BotsRosterComponent,
    DeployDialogComponent,
    ButtonModule,
  ],
  templateUrl: './bots-list-page.component.html',
  styleUrl: './bots-list-page.component.scss',
  host: { class: 'block h-full' },
})
export class BotsListPageComponent {
  // Route params bound via withComponentInputBinding()
  readonly broker = input('alpaca');
  readonly accountId = input<string | undefined>(undefined);

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly refreshEpoch = signal(0);

  protected readonly deployVisible = signal(false);
  protected readonly actionError = signal<string | null>(null);

  protected readonly catalog = resource({
    params: () => ({
      broker: this.broker(),
      accountId: this.accountId(),
      epoch: this.refreshEpoch(),
    }),
    loader: ({
      params,
    }: {
      params: { broker: string; accountId: string | undefined; epoch: number };
    }) => this.panelService.getCatalog(params.broker, params.accountId),
  });

  protected readonly profile = resource({
    params: () => this.broker(),
    loader: ({ params }: { params: string }) =>
      this.panelService.getPanelProfile(params),
  });

  constructor() {
    const timer = setInterval(() => {
      this.refreshEpoch.update((epoch) => epoch + 1);
    }, 5_000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  protected onDeployed(): void {
    this.catalog.reload();
  }

  protected async onRowAction(event: RowActionEvent): Promise<void> {
    this.actionError.set(null);
    const accountId = this.accountId() ?? event.bot.account_id;
    const idempotencyKey = crypto.randomUUID();
    const revision = 0; // the catalog view does not surface revision; the backend validates

    const request: PanelActionRequest = {
      action_id: event.action,
      revision,
      idempotency_key: idempotencyKey,
    };

    try {
      await this.panelService.runAction(
        this.broker(),
        accountId,
        event.bot.strategy_instance_id,
        request,
      );
      this.catalog.reload();
    } catch {
      this.actionError.set(
        `Action "${event.action}" failed for bot ${event.bot.strategy_instance_id}.`,
      );
    }
  }

  protected get bots(): BotCatalogView[] {
    return this.catalog.value() ?? [];
  }
}
