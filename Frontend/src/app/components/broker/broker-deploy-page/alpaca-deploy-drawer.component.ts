import { ChangeDetectionStrategy, Component, computed, inject, input, output, resource } from '@angular/core';
import { Drawer } from 'primeng/drawer';

import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from '../../brokers/alpaca-desk/alpaca-desk-account-data.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';

/**
 * Reusable right-side host for the established Alpaca deploy workflow.
 *
 * Route surfaces own visibility: the desk mirrors it in the URL while an
 * account-scoped Bots list keeps it as local page state. The workflow itself
 * remains the sole owner of deployment form state and submission behavior.
 */
@Component({
  selector: 'app-alpaca-deploy-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, AlpacaDeployWorkflowComponent],
  templateUrl: './alpaca-deploy-drawer.component.html',
  styleUrl: './alpaca-deploy-drawer.component.scss',
})
export class AlpacaDeployDrawerComponent {
  readonly visible = input.required<boolean>();
  readonly accountId = input('');
  readonly closed = output();

  private readonly brokers = inject(BrokersService);
  private readonly deskAccountData = inject(AlpacaDeskAccountDataService, { optional: true });

  protected readonly account = resource({
    params: () => (
      this.visible() && this.accountId().trim() === '' && this.deskAccountData === null
        ? 'alpaca'
        : undefined
    ),
    loader: ({ params }) => this.brokers.getAccount(params),
  });

  protected readonly resolvedAccountId = computed(() => {
    const explicitAccountId = this.accountId().trim();
    if (explicitAccountId) return explicitAccountId;
    const sharedAccount = this.deskAccountData?.account;
    if (sharedAccount?.hasValue()) return sharedAccount.value().account_id;
    return this.account.hasValue() ? this.account.value().account_id : '';
  });

  protected readonly accountUnavailable = computed(
    () => this.deskAccountData?.account.error() !== undefined || this.account.error() !== undefined,
  );

  /** `null` while no account read has answered, or the account is live: the chrome then names no world. */
  private readonly brokerWorld = computed<'paper' | null>(() => {
    const shared = this.deskAccountData?.account;
    const observed = shared?.hasValue()
      ? shared.value()
      : this.account.hasValue()
        ? this.account.value()
        : null;
    // A paper account deploys Paper. A live account is shadowed or
    // live-custodied; the drawer does not know which until the deploy view
    // says, so it names no world rather than a wrong one (ADR 0059 D2/D11).
    return observed?.account_mode === 'paper' ? 'paper' : null;
  });

  protected readonly headerLabel = computed(() => {
    const account = this.resolvedAccountId() || 'Alpaca';
    const world = this.brokerWorld();
    return world === null ? `Deploy · ${account}` : `Deploy · ${account} · ${world}`;
  });

  protected readonly accountNoun = computed(() => {
    const world = this.brokerWorld();
    return world === null ? 'Alpaca account' : `Alpaca ${world} account`;
  });

  protected close(): void {
    this.closed.emit();
  }

  protected onVisibilityChange(visible: boolean): void {
    if (!visible) this.close();
  }
}
