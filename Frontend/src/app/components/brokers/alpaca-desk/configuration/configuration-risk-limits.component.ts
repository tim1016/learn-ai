import { CurrencyPipe, PercentPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, resource } from '@angular/core';
import { FormField, form } from '@angular/forms/signals';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { resourceTarget } from '../../../../fleet/resource-target';
import { BrokerConfigurationService } from './broker-configuration.service';
import { ConfigurationRefusalComponent } from './configuration-refusal.component';
import { type ConfigurationRefusal, toConfigurationRefusal } from './broker-configuration-refusal';

@Component({
  selector: 'app-configuration-risk-limits',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField, CurrencyPipe, PercentPipe, ConfigurationRefusalComponent],
  templateUrl: './configuration-risk-limits.component.html',
  styleUrl: './configuration-risk-limits.component.scss',
})
export class ConfigurationRiskLimitsComponent {
  readonly clerkId = input.required<string>();
  private readonly service = inject(BrokerConfigurationService);
  private readonly directory = inject(FleetDirectoryService);
  protected readonly state = resource({
    params: () => this.clerkId(),
    loader: ({ params }) => this.service.readRiskLimits(params),
  });
  protected readonly draft = linkedSignal(() => ({
    loss_fraction: this.state.hasValue() ? this.state.value().loss_fraction : null,
    loss_usd: this.state.hasValue() ? this.state.value().loss_usd : null,
  }));
  protected readonly fields = form(this.draft);
  protected readonly busy = linkedSignal(() => { this.clerkId(); return false; });
  protected readonly refusal = linkedSignal<string, ConfigurationRefusal | null>({ source: this.clerkId, computation: () => null });
  protected readonly applied = linkedSignal(() => { this.clerkId(); return false; });
  protected readonly valid = computed(() => {
    const draft = this.draft();
    return draft.loss_fraction !== null && draft.loss_fraction > 0 && draft.loss_fraction < 1
      && draft.loss_usd !== null && draft.loss_usd > 0;
  });

  protected async apply(clearHold = false): Promise<void> {
    const state = this.state.hasValue() ? this.state.value() : null;
    const draft = this.draft();
    if (this.busy() || state === null || draft.loss_fraction === null || draft.loss_usd === null || (!clearHold && !this.valid())) return;
    const clerkId = this.clerkId();
    const lane = this.directory.lane('alpaca', clerkId);
    const target = resourceTarget('alpaca', clerkId, {
      capability: 'configuration_manage', idempotencyKey: globalThis.crypto.randomUUID(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
    this.busy.set(true);
    this.refusal.set(null);
    this.applied.set(false);
    try {
      const reviewed = {
        expected_risk_revision: state.risk_revision,
        expected_selection_generation: state.selection_generation,
      };
      const updated = clearHold
        ? await this.service.clearRiskHold(target, reviewed)
        : await this.service.applyRiskLimits(target, {
          ...reviewed, loss_fraction: draft.loss_fraction, loss_usd: draft.loss_usd,
        });
      if (this.clerkId() !== clerkId) return;
      this.state.set(updated);
      this.applied.set(!clearHold);
    } catch (error) {
      if (this.clerkId() === clerkId) this.refusal.set(toConfigurationRefusal(error));
    } finally {
      if (this.clerkId() === clerkId) this.busy.set(false);
    }
  }
}
