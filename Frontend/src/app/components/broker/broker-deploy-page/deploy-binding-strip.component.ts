import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';
import { TooltipModule } from 'primeng/tooltip';

import { ExperimentalNoticeComponent } from '../../../shared/experimental-notice/experimental-notice.component';
import type { DeployBotStrategy } from '../v2-panel/lib/broker-v2-panel.service';

/** Deploy's strategy choice and its evidence, and the registry's warning for
 * a strategy that is not a trading strategy (#2607). The bot's name is not
 * asked for: the backend authors it at Deploy (#2551). */
@Component({
  selector: 'app-deploy-binding-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ExperimentalNoticeComponent, RouterLink, TooltipModule],
  templateUrl: './deploy-binding-strip.component.html',
  styleUrl: './deploy-binding-strip.component.scss',
})
export class DeployBindingStripComponent {
  readonly strategies = input.required<DeployBotStrategy[]>();
  readonly strategyKey = input.required<DeployBotStrategy['strategy_key'] | ''>();

  readonly strategyKeyChange = output<DeployBotStrategy['strategy_key']>();

  protected readonly selectedStrategy = computed(() =>
    this.strategies().find((strategy) => strategy.strategy_key === this.strategyKey()) ?? null,
  );

  protected changeStrategy(event: Event): void {
    if (event.target instanceof HTMLSelectElement) {
      const strategyKey = event.target.value;
      const strategy = this.strategies().find(
        (candidate) => candidate.strategy_key === strategyKey,
      );
      if (strategy) this.strategyKeyChange.emit(strategy.strategy_key);
    }
  }
}
