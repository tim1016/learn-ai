import { LowerCasePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import { QUALIFICATION_LIST_LIMIT } from '../../components/golden-search/golden-search.service';
import type { GoldenQualificationSummary } from '../../components/golden-search/golden-search.types';
import { GOLDEN_DEPLOY_HANDOFF_ROUTE } from '../../fleet/account-workspace';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { deployHandoffQuery } from '../golden/golden-tabs';
import { SignalStrategyChipComponent } from '../signal-strategy-chip.component';
import { QUALIFICATION_TONES, parameterPairs } from '../signal-strategy-evidence';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';

/**
 * What the header's "Deploy a bot" opens (PRD #3490 R19, I02): the choice of what
 * to carry to the account list. Nothing here starts a bot or reads an account.
 * Each link opens the account list with Deploy's own query, and the owner picks
 * the account there.
 *
 * - **A qualified version.** A ready version links with its own id, so the exact
 *   version, with its symbol and configuration, is what travels. The current
 *   default is marked where it is displayed and linked the same way, by the id
 *   shown: the owner either picks any ready version or explicitly takes the
 *   default, and no link names "the default" for the server to resolve later.
 *   A version that is stale, revoked or unreadable is listed with why and has no
 *   link; nothing offers the next one in its place.
 * - **The strategy alone**, for the registered corpus point: it needs no saved
 *   version, so none is required of it.
 *
 * Deploy re-checks the version, the account and its gates at its own boundary.
 */
@Component({
  selector: 'app-signal-strategy-deploy',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    LowerCasePipe,
    ReceiptLabelPipe,
    RouterLink,
    SignalStrategyChipComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './signal-strategy-deploy.component.html',
  styleUrls: ['../section-content.scss', './signal-strategy-deploy.component.scss'],
})
export class SignalStrategyDeployComponent {
  protected readonly store = inject(SignalStrategyWorkspaceStore);

  readonly closed = output();

  protected readonly accountListRoute = GOLDEN_DEPLOY_HANDOFF_ROUTE;
  protected readonly tones = QUALIFICATION_TONES;
  protected readonly listLimit = QUALIFICATION_LIST_LIMIT;

  protected readonly strategyOnlyQuery = computed(() => deployHandoffQuery(this.store.key(), null));

  /** Why Golden Search is ruled out for this strategy, when the registry says so. */
  protected readonly goldenRefusal = computed(() => {
    const state = this.store.entry();
    return state.kind === 'found' && !state.entry.capabilities.golden.supported ? state.entry.capabilities.golden.reason : null;
  });

  protected readonly gate = computed<WorkspaceStateContent | null>(() => {
    const state = this.store.qualifiedVersions();
    if (state.kind === 'ready') return null;
    if (state.kind === 'loading') return { kind: 'checking', happened: 'Reading this strategy’s qualified versions.' };
    return {
      kind: 'failed',
      happened: 'This strategy’s qualified versions could not be read.',
      why: state.message,
      next: 'No version can be chosen until they are read. The strategy can still be deployed without a saved version, below.',
      actions: [{ id: 'retry', label: 'Read them again' }],
    };
  });

  constructor() {
    this.store.want('approvals');
  }

  protected versionQuery(version: GoldenQualificationSummary): Record<string, string> {
    return deployHandoffQuery(version.program_key, version.id);
  }

  protected parameters(version: GoldenQualificationSummary): string {
    return parameterPairs(version.parameters)?.join(' · ') ?? 'no settings recorded';
  }

  protected retry(): void {
    this.store.retry('approvals');
  }
}
