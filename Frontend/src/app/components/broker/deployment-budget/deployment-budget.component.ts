import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { MoneyBarComponent } from '../money-bar/money-bar.component';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';

let nextHeadingId = 0;

/**
 * The bot page's "This bot's money" card (PRD #2560 D6, stories 42–43).
 *
 * Python authors the headline, the detail, every statement line and every
 * dollar; this card renders them verbatim and in order, and does no money
 * arithmetic. The slice is the one Home draws for this bot (`segment`), as
 * Python wrote it: a running bot's balance shaded by its parts, a stopped
 * bot's still-held money with what it released; a finished bot has none.
 * Open gain or loss on shares is a note, never in the bar.
 */
@Component({
  selector: 'app-deployment-budget',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, MoneyBarComponent, TimestampDisplayComponent],
  templateUrl: './deployment-budget.component.html',
  styleUrl: './deployment-budget.component.scss',
})
export class DeploymentBudgetComponent {
  readonly target = input.required<ResourceTarget>();
  readonly strategyInstanceId = input.required<string>();
  readonly revision = input(0);
  readonly holdsShares = input(false);
  /** The bot panel's open gain or loss on shares; null when there is no current price. */
  readonly openPnl = input<number | null>(null);
  /**
   * The same figure as the panel formats it: a money surface hands no value
   * to a pipe that formats numbers (PRD #2560 D12).
   */
  readonly openPnlText = input<string | null>(null);

  protected readonly headingId = `deployment-budget-heading-${nextHeadingId++}`;
  private readonly service = inject(BrokerV2PanelService);
  protected readonly budget = resource({
    params: () => ({ target: this.target(), sid: this.strategyInstanceId(), revision: this.revision() }),
    loader: ({ params }) => this.service.getBudget(params.target, params.sid),
  });
  protected readonly view = computed(() => this.budget.hasValue() ? this.budget.value() : null);
  protected readonly statement = computed(() => this.view()?.statement ?? []);
}
