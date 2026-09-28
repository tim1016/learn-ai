import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, resource, signal } from '@angular/core';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { resourceTarget, withCommand, type ResourceTarget } from '../../../../fleet/resource-target';
import { sameAlpacaAccount } from '../../../../services/alpaca-account-identity';
import { BrokerConfigurationService } from './broker-configuration.service';
import { type ConfigurationRefusal, toConfigurationRefusal } from './broker-configuration-refusal';
import { ConfigurationRefusalComponent } from './configuration-refusal.component';

@Component({
  selector: 'app-configuration-budget-authority',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ConfigurationRefusalComponent],
  templateUrl: './configuration-budget-authority.component.html',
  styleUrl: './configuration-budget-authority.component.scss',
})
export class ConfigurationBudgetAuthorityComponent {
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  private readonly service = inject(BrokerConfigurationService);
  private readonly directory = inject(FleetDirectoryService);
  private readonly target = computed(() => {
    const clerkId = this.clerkId();
    const lane = this.directory.lane('alpaca', clerkId);
    if (!lane || lane.effective_binding_generation === null || lane.routing_epoch === null) return null;
    return resourceTarget('alpaca', clerkId, { accountId: this.accountId(),
      bindingGeneration: lane.effective_binding_generation, routingEpoch: lane.routing_epoch });
  });
  protected readonly state = resource({
    params: () => this.target() ?? undefined,
    loader: async ({ params }) => ({ target: params, state: await this.service.readBudgetAuthority(params) }),
  });
  protected readonly view = computed(() => this.state.hasValue() ? this.state.value().state : null);
  protected readonly busy = linkedSignal(() => { this.target(); return false; });
  protected readonly refusal = linkedSignal<ResourceTarget | null, ConfigurationRefusal | null>({ source: this.target, computation: () => null });
  private readonly command = signal<{ key: string; target: ResourceTarget } | null>(null);
  protected readonly canApply = computed(() => {
    const state = this.view();
    return !this.busy() && !this.state.isLoading() && state?.state === 'legacy'
      && !!state.review_token && sameAlpacaAccount(state.account_id, this.accountId());
  });

  protected async apply(): Promise<void> {
    if (!this.canApply() || !this.state.hasValue()) return;
    const reviewed = this.state.value();
    const key = JSON.stringify({ target: reviewed.target, token: reviewed.state.review_token });
    const previous = this.command();
    const target = previous?.key === key ? previous.target
      : withCommand(reviewed.target, 'configuration_manage', crypto.randomUUID());
    this.command.set({ key, target });
    this.busy.set(true);
    this.refusal.set(null);
    try {
      const state = await this.service.applyBudgetAuthority(target, { review_token: reviewed.state.review_token });
      if (this.target() !== reviewed.target) return;
      this.state.set({ target: reviewed.target, state });
      this.command.set(null);
    } catch (error) {
      if (this.target() === reviewed.target) {
        this.refusal.set(toConfigurationRefusal(error));
        // A changed review must be read again, never silently submitted under
        // a newer token. Unknown outcomes remain read-only until explicit retry.
        this.state.reload();
      }
    } finally {
      if (this.target() === reviewed.target) this.busy.set(false);
    }
  }
}
