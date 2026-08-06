import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';
import { InputTextModule } from 'primeng/inputtext';
import { TooltipModule } from 'primeng/tooltip';

import type { DeployBotStrategy } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-binding-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, InputTextModule, TooltipModule],
  templateUrl: './deploy-binding-strip.component.html',
  styleUrl: './deploy-binding-strip.component.scss',
})
export class DeployBindingStripComponent {
  readonly strategies = input.required<DeployBotStrategy[]>();
  readonly instanceId = input.required<string>();
  readonly strategyKey = input.required<DeployBotStrategy['strategy_key'] | ''>();
  readonly instanceIdError = input<string | null>(null);

  readonly instanceIdChange = output<string>();
  readonly instanceIdBlur = output();
  readonly strategyKeyChange = output<DeployBotStrategy['strategy_key']>();

  protected readonly selectedStrategy = computed(() =>
    this.strategies().find((strategy) => strategy.strategy_key === this.strategyKey()) ?? null,
  );

  protected changeInstanceId(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.instanceIdChange.emit(event.target.value);
    }
  }

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
