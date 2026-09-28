import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { resourceTarget, withCommand, type ResourceTarget } from '../../../../fleet/resource-target';
import { sameAlpacaAccount } from '../../../../services/alpaca-account-identity';
import { BrokerConfigurationService } from './broker-configuration.service';
import { type ConfigurationRefusal, toConfigurationRefusal } from './broker-configuration-refusal';
import { ConfigurationRefusalComponent } from './configuration-refusal.component';

/**
 * The Budgets section of Settings (PRD #2560): the one-time switch to budgets.
 *
 * The switch stops the lane's bots and cannot be undone, so it asks first, in
 * place: what changes, and that it is permanent (#2566, H10). Once the account
 * uses budgets — the backend's own state, not this page's memory — the card is
 * gone and one line says so.
 */
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
  private readonly injector = inject(Injector);
  private readonly opener = viewChild<ElementRef<HTMLButtonElement>>('opener');
  private readonly confirmHeading = viewChild<ElementRef<HTMLElement>>('confirmHeading');
  private readonly budgetsOn = viewChild<ElementRef<HTMLElement>>('budgetsOn');
  private readonly refusalRegion = viewChild<ElementRef<HTMLElement>>('refusalRegion');
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
  /** The inline "cannot be undone" step is open. */
  protected readonly confirming = linkedSignal(() => { this.target(); return false; });
  protected readonly refusal = linkedSignal<ResourceTarget | null, ConfigurationRefusal | null>({ source: this.target, computation: () => null });
  private readonly command = signal<{ key: string; target: ResourceTarget } | null>(null);
  protected readonly canApply = computed(() => {
    const state = this.view();
    return !this.busy() && !this.state.isLoading() && state?.state === 'legacy'
      && !!state.review_token && sameAlpacaAccount(state.account_id, this.accountId());
  });

  /** What the switch stops, in words, from the backend's own run count. */
  protected readonly stopsCopy = computed(() => {
    const count = this.view()?.active_run_count ?? 0;
    if (count === 0) return 'No bots are running, so none will stop.';
    return count === 1 ? 'The 1 running bot stops now.' : `The ${count} running bots stop now.`;
  });

  protected review(): void {
    this.confirming.set(true);
    this.focusAfterRender(this.confirmHeading);
  }

  protected cancel(): void {
    this.confirming.set(false);
    this.focusAfterRender(this.opener);
  }

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
      this.confirming.set(false);
      this.focusAfterRender(this.budgetsOn);
    } catch (error) {
      if (this.target() === reviewed.target) {
        this.refusal.set(toConfigurationRefusal(error));
        this.focusAfterRender(this.refusalRegion);
        // A changed review must be read again, never silently submitted under
        // a newer token. Unknown outcomes remain read-only until explicit retry.
        this.state.reload();
      }
    } finally {
      if (this.target() === reviewed.target) this.busy.set(false);
    }
  }

  private focusAfterRender(region: () => ElementRef<HTMLElement> | undefined): void {
    afterNextRender({ write: () => region()?.nativeElement.focus() }, { injector: this.injector });
  }
}
