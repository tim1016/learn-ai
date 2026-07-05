import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { StrategyValidationService } from '../../services/strategy-validation.service';
import type {
  StrategyValidationDetail,
  StrategyValidationSummary,
} from '../../services/strategy-validation.types';

@Component({
  selector: 'app-strategy-validation',
  templateUrl: './strategy-validation.component.html',
  styleUrl: './strategy-validation.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyValidationComponent {
  private readonly service = inject(StrategyValidationService);
  private readonly selectedOverride = signal<string | null>(null);

  protected readonly catalog = resource({
    loader: () => this.service.getCatalog(),
  });

  protected readonly strategies = computed(() => this.catalog.value()?.strategies ?? []);
  protected readonly validatedCount = computed(
    () => this.strategies().filter((strategy) => strategy.deployable).length,
  );
  protected readonly selectedKey = computed(
    () => this.selectedOverride() ?? this.strategies()[0]?.strategy_key ?? null,
  );

  protected readonly detail = resource<StrategyValidationDetail | null, string | null>({
    params: () => this.selectedKey(),
    loader: ({ params }) => {
      if (params === null) return Promise.resolve(null);
      return this.service.getDetail(params);
    },
  });

  protected selectStrategy(strategy: StrategyValidationSummary): void {
    this.selectedOverride.set(strategy.strategy_key);
  }

  protected stateLabel(strategy: StrategyValidationSummary | StrategyValidationDetail): string {
    return strategy.validation_state === 'validated' ? 'Validated' : 'Needs validation';
  }

  protected isSelected(strategy: StrategyValidationSummary): boolean {
    return strategy.strategy_key === this.selectedKey();
  }

  protected divergenceEntries(detail: StrategyValidationDetail): [string, number][] {
    return Object.entries(detail.diagnostics?.divergence_counts ?? {});
  }
}
