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

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotCatalogView } from '../lib/broker-v2-panel.types';
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
  readonly accountId = input.required<string>();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly deployVisible = signal(false);
  protected readonly actionError = signal<string | null>(null);

  protected readonly catalog = resource({
    params: () => ({
      broker: this.broker(),
      accountId: this.accountId(),
    }),
    loader: ({
      params,
    }: {
      params: { broker: string; accountId: string };
    }) => this.panelService.getCatalog(params.broker, params.accountId),
  });

  protected readonly profile = resource({
    params: () => this.broker(),
    loader: ({ params }: { params: string }) =>
      this.panelService.getPanelProfile(params),
  });

  constructor() {
    // Poll via reload(): keeps the previous value while refreshing and
    // no-ops when a load is already in flight. Driving the refresh through
    // a params-epoch instead aborts and resets the resource each tick,
    // which never converges when the endpoint is slower than the interval.
    const timer = setInterval(() => {
      this.catalog.reload();
    }, 5_000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  protected onDeployed(): void {
    this.catalog.reload();
  }

  protected async onRowAction(event: RowActionEvent): Promise<void> {
    this.actionError.set(null);
    const broker = this.broker();
    const accountId = this.accountId();
    const sid = event.bot.strategy_instance_id;

    try {
      // Fetch panel to get the current revision and presented-action state.
      const panel = await this.panelService.getPanel(broker, accountId, sid);
      const action = panel.actions.find((a) => a.action_id === event.action);
      if (!action?.enabled) {
        this.actionError.set(`Action "${event.action}" is not available for bot ${sid}.`);
        return;
      }
      await this.panelService.runBotAction(broker, accountId, sid, action);
      this.catalog.reload();
    } catch {
      this.actionError.set(`Action "${event.action}" failed for bot ${sid}.`);
    }
  }

  protected get bots(): BotCatalogView[] {
    return this.catalog.value() ?? [];
  }
}
