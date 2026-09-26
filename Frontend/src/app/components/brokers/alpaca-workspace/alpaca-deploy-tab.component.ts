import { ChangeDetectionStrategy, Component, computed, effect, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';

import { AlpacaDeployWorkflowComponent } from '../../broker/broker-deploy-page/alpaca-deploy-workflow.component';
import { DeployResumeBotsComponent } from '../../broker/broker-deploy-page/deploy-resume-bots.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';

/** Why Deploy is unavailable, in the order the operator can act on: no lane
 * to target, then no declared capability, then no confirmed account.
 * Capability evidence is the provider's, never inferred (FR-097). */
const DEPLOY_WITHOUT_LANE = 'This account’s lane has not resolved, so Deploy has no clerk to target.';
const DEPLOY_WITHOUT_CAPABILITY = 'This clerk does not declare Deploy capability.';
const DEPLOY_WITHOUT_ACCOUNT = 'Alpaca has not confirmed this account yet.';

/**
 * The Deploy tab (ADR 0064 Decision 1, extended): binds a validated strategy
 * to this account inline, in the tab strip, rather than as an overlay drawer.
 *
 * Reads the workspace shell's own `AlpacaDeskAccountDataService` instance —
 * provided on `AlpacaAccountWorkspaceComponent` and inherited by every tab
 * under it — so this tab can never target a different account than the one
 * the header names. Blocked-state messaging mirrors the other lane-scoped
 * tabs (Bots, Gallery): the tab always has a route once an account is
 * confirmed, and explains in place why it cannot deploy rather than being
 * hidden or disabled from the tab strip (FR-096).
 */
@Component({
  selector: 'app-alpaca-deploy-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaDeployWorkflowComponent, DeployResumeBotsComponent],
  templateUrl: './alpaca-deploy-tab.component.html',
  styleUrl: './alpaca-deploy-tab.component.scss',
})
export class AlpacaDeployTabComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly routeParams = toSignal(this.route.paramMap, {
    initialValue: this.route.snapshot.paramMap,
  });

  private readonly clerkId = computed(() => this.routeParams().get('clerkId') ?? '');
  private readonly lane = computed(() => this.fleetDirectory.lane('alpaca', this.clerkId()) ?? null);

  private readonly confirmedAccount = signal<string | null>(null);

  constructor() {
    effect(() => {
      if (this.accountData.account.hasValue()) this.confirmedAccount.set(this.accountData.accountId());
    });
  }

  protected readonly blockedReason = computed(() => {
    const lane = this.lane();
    if (lane === null) return DEPLOY_WITHOUT_LANE;
    if (!lane.capabilities.includes('deploy')) return DEPLOY_WITHOUT_CAPABILITY;
    return this.accountData.account.hasValue() || this.confirmedAccount() === this.accountData.accountId()
      ? null : DEPLOY_WITHOUT_ACCOUNT;
  });

  protected readonly target = this.accountData.target;
  protected readonly accountId = this.accountData.accountId;
  protected readonly fence = this.accountData.fence;
}
