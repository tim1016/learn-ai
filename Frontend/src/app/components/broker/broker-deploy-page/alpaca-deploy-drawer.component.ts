import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';
import { Drawer } from 'primeng/drawer';

import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from '../../brokers/alpaca-desk/alpaca-desk-account-data.service';
import type { ResourceTarget } from '../../../fleet/resource-target';
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
  readonly target = input<ResourceTarget | null>(null);
  readonly closed = output();

  private readonly brokers = inject(BrokersService);
  private readonly deskAccountData = inject(AlpacaDeskAccountDataService, { optional: true });
  private readonly candidateTarget = computed(
    () => this.target() ?? this.deskAccountData?.target() ?? null,
  );
  private readonly frozenTarget = signal<ResourceTarget | null>(null);
  private wasVisible = false;
  /** The target is captured when the drawer opens, never recalculated while
   * a preview or submit is in flight. */
  protected readonly resolvedTarget = this.frozenTarget.asReadonly();

  protected readonly account = resource({
    params: () => this.visible() && this.accountId().trim() === '' ? this.resolvedTarget() : null,
    loader: ({ params }) => params === null
      ? Promise.reject(new Error('Deploy requires a routed clerk target.'))
      : this.brokers.getAccount(params),
  });

  protected readonly resolvedAccountId = computed(() => {
    const explicitAccountId = this.accountId().trim();
    if (explicitAccountId) return explicitAccountId;
    const sharedAccount = this.deskAccountData?.account;
    if (sharedAccount?.hasValue()) return sharedAccount.value().account_id;
    return this.account.hasValue() ? this.account.value().account_id : '';
  });

  protected readonly accountUnavailable = computed(
    () => this.resolvedTarget() === null
      || this.deskAccountData?.account.error() !== undefined
      || this.account.error() !== undefined,
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

  constructor() {
    effect(() => {
      const visible = this.visible();
      if (visible && !this.wasVisible) this.frozenTarget.set(this.candidateTarget());
      if (!visible) this.frozenTarget.set(null);
      this.wasVisible = visible;
    });
  }

  protected close(): void {
    this.closed.emit();
  }

  protected onVisibilityChange(visible: boolean): void {
    if (!visible) this.close();
  }
}
