import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';
import { DEPLOY_WORLDS, deployWorldOf } from '../broker-deploy-page/deploy-world';

@Component({
  selector: 'app-deployment-budget',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, TimestampDisplayComponent],
  templateUrl: './deployment-budget.component.html',
  styleUrl: './deployment-budget.component.scss',
})
export class DeploymentBudgetComponent {
  readonly target = input.required<ResourceTarget>();
  readonly strategyInstanceId = input.required<string>();
  readonly revision = input(0);
  private readonly service = inject(BrokerV2PanelService);
  protected readonly budget = resource({
    params: () => ({ target: this.target(), sid: this.strategyInstanceId(), revision: this.revision() }),
    loader: ({ params }) => this.service.getBudget(params.target, params.sid),
  });
  protected readonly view = computed(() => this.budget.hasValue() ? this.budget.value() : null);
  protected readonly worldName = computed(() => {
    const view = this.view();
    return view === null ? '' : DEPLOY_WORLDS[deployWorldOf(view.world)].name;
  });
  protected readonly amounts = computed(() => {
    const view = this.view();
    return view === null ? [] : [
      { label: 'Original committed budget', amount: view.committed_usd },
      { label: 'Realized gains / losses before fees', amount: view.realized_gross_usd },
      { label: 'Attributed fees', amount: view.fees_usd },
      { label: 'Cost held in positions', amount: view.position_cost_usd },
      { label: 'Pending entry orders', amount: view.pending_orders_usd },
      { label: 'Free budget', amount: view.free_usd },
      { label: 'Shortfall', amount: view.shortfall_usd },
      { label: 'Cash released', amount: view.released_usd },
      { label: 'Cash still claimed by orders, fills or fees', amount: view.outstanding_cash_usd },
    ];
  });
}
