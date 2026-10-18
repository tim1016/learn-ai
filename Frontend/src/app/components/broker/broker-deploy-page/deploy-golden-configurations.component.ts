import { ChangeDetectionStrategy, Component, computed, inject, input, output, resource, signal } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { GoldenSearchService } from '../../golden-search/golden-search.service';
import { extractServerMessage } from '../operation-error';
import type { DeployBotView } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-golden-configurations',
  imports: [AssetIdentityComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './deploy-golden-configurations.component.html',
  styleUrl: './deploy-parameters-section.component.scss',
})
export class DeployGoldenConfigurationsComponent {
  readonly strategies = input.required<DeployBotView['strategies']>();
  readonly locked = input(false);
  readonly selected = output<string>();
  readonly refreshed = output();
  private readonly service = inject(GoldenSearchService);
  protected readonly versions = resource({ loader: () => this.service.listQualifications() });
  protected readonly selectedId = signal('');
  protected readonly rechecking = signal(false);
  protected readonly problem = signal<string | null>(null);
  protected readonly loadProblem = computed(() => this.versions.error() === undefined ? null
    : extractServerMessage(this.versions.error(), 'Saved golden configurations could not be read.'));
  protected readonly choices = computed(() => (this.versions.hasValue() ? this.versions.value() : []).map(version => ({
    ...version, label: this.strategies().find(strategy => strategy.strategy_key === version.program_key)?.label ?? version.program_key,
  })));
  protected readonly version = computed(() => this.choices().find(version => version.id === this.selectedId()) ?? null);
  protected readonly offered = computed(() => this.strategies().some(strategy => strategy.strategy_key === this.version()?.program_key));
  protected readonly parameters = computed(() => {
    const version = this.version();
    const schema = this.strategies().find(strategy => strategy.strategy_key === version?.program_key)?.params_schema;
    return Object.entries(version?.parameters ?? {}).map(([key, value]) => ({ label: schema?.properties?.[key]?.title ?? key, value }));
  });

  protected select(target: EventTarget | null): void {
    if (!(target instanceof HTMLSelectElement) || this.locked() || this.rechecking()) return;
    this.selectedId.set(target.value);
    this.problem.set(null);
  }

  protected apply(): void {
    const version = this.version();
    if (!this.locked() && !this.rechecking() && this.offered() && version?.status === 'ready') this.selected.emit(version.id);
  }

  protected async recheck(): Promise<void> {
    const version = this.version();
    if (this.locked() || this.rechecking() || version?.status !== 'stale') return;
    this.rechecking.set(true);
    this.problem.set(null);
    try {
      await this.service.reproveQualification(version.id, crypto.randomUUID());
      this.versions.reload();
      this.refreshed.emit();
    } catch (error) {
      this.problem.set(extractServerMessage(error, 'The saved configuration could not be revalidated.'));
    } finally {
      this.rechecking.set(false);
    }
  }
}
