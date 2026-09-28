import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';

import { AlpacaDeployWorkflowComponent } from '../../broker/broker-deploy-page/alpaca-deploy-workflow.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneFenceDrifted } from '../../../fleet/lane-fence';

/** Why Deploy is unavailable, in the order the operator can act on: no lane
 * to target, then no declared capability, then no confirmed account.
 * Capability evidence is the provider's, never inferred (FR-097). */
const DEPLOY_WITHOUT_LANE = 'This account’s lane has not resolved, so Deploy has no clerk to target.';
const DEPLOY_WITHOUT_CAPABILITY = 'This clerk does not declare Deploy capability.';
const DEPLOY_WITHOUT_ACCOUNT = 'Alpaca has not confirmed this account yet.';

/**
 * The Deploy page (ADR 0064 Decision 1, extended; PRD #2560 D3): opened from
 * the header's "Deploy a bot" button at its routed `deploy` URL — no longer a
 * tab in the strip — and never an overlay drawer.
 *
 * Reads the workspace shell's own `AlpacaDeskAccountDataService` instance —
 * provided on `AlpacaAccountWorkspaceComponent` and inherited by every tab
 * under it — so this page can never target a different account than the one
 * the header names. The header offers the button only once Alpaca has
 * confirmed the account (an accountless lane has nowhere to open Deploy);
 * reached by its URL without a lane, a capability or an account, this page
 * explains in place why it cannot deploy, as Bots and Gallery do (FR-096).
 */
@Component({
  selector: 'app-alpaca-deploy-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaDeployWorkflowComponent],
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

  protected readonly blockedReason = computed(() => {
    const lane = this.lane();
    if (lane === null) return DEPLOY_WITHOUT_LANE;
    if (!lane.capabilities.includes('deploy')) return DEPLOY_WITHOUT_CAPABILITY;
    return this.accountData.account.hasValue()
      ? null : DEPLOY_WITHOUT_ACCOUNT;
  });

  protected readonly target = this.accountData.target;
  protected readonly accountId = this.accountData.accountId;
  protected readonly fence = this.accountData.fence;
  protected readonly laneReviewRequired = computed(() => laneFenceDrifted(this.fence(), this.lane() ?? undefined));
  protected reviewCurrentLane(): void { this.accountData.reviewCurrentLane(); }
}
