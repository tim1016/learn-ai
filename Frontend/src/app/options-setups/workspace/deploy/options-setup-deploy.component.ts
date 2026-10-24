import { ChangeDetectionStrategy, Component, computed, inject, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import { GOLDEN_DEPLOY_HANDOFF_ROUTE } from '../../../fleet/account-workspace';
import { optionsDeployHandoffQuery } from '../../../fleet/options-setup-handoff';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { OptionsSetupEvidenceStore } from '../../index/options-setup-evidence-store';
import { optionsEvidenceRoute, optionsSetupRoute } from '../../options-setups.address';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { OptionsSetupVersions } from '../options-setup-versions';
import { PATH_TITLE, type ListedVersion } from '../golden/golden-versions';

/**
 * What the setup header's "Deploy an options bot" opens (PRD #3490 R23, I02): the choice of
 * which approved version to carry to the account list. It is the options twin of the signal
 * side's "Deploy a bot", and nothing here starts a bot or reads an account. Each ready version
 * links to the account list with Deploy's own `options_setup` key, by its own id, and the owner
 * picks the account there.
 *
 * - **Versions are found the way Golden finds them**: by the exact content this setup is saved
 *   as, and, apart from those, the versions a search that began from it made with changed settings.
 *   Each is read by its own id, and a link is offered only when that read says it is ready.
 * - **There is no default.** One ready version is still the owner's click; nothing is pre-chosen.
 * - **A version that is stale, revoked or unreadable** is listed with why and has no link. Nothing
 *   offers the next one in its place.
 * - **A registered signal program that trades options is not here.** It deploys from its own
 *   workflow: the split is the strategy model, never the asset traded.
 *
 * Deploy re-checks the version, the account and its gates at its own boundary. The versions are the ones Golden
 * lists, read once for the workspace; opening the choice reads what each says now again, because a link is offered
 * only for a version that is ready at this moment.
 */
@Component({
  selector: 'app-options-setup-deploy',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, ReceiptLabelPipe, RouterLink, WorkspaceStateBlockComponent],
  templateUrl: './options-setup-deploy.component.html',
  styleUrl: './options-setup-deploy.component.scss',
})
export class OptionsSetupDeployComponent {
  private readonly store = inject(OptionsSetupWorkspaceStore);
  private readonly evidence = inject(OptionsSetupEvidenceStore);
  private readonly versionStore = inject(OptionsSetupVersions);

  readonly closed = output();

  protected readonly accountListRoute = GOLDEN_DEPLOY_HANDOFF_ROUTE;
  protected readonly evidenceRoute = optionsEvidenceRoute;
  protected readonly pathTitle = PATH_TITLE;
  protected readonly goldenRoute = computed(() => optionsSetupRoute(this.store.address(), 'golden'));
  /** Versions are of the content as last saved: an edit in Define that is not saved changes none of them. */
  protected readonly draftChanged = computed(() => this.store.standing().kind === 'changed');

  /** The approved versions of the saved content, then those a search that began from it made with changed settings. */
  protected readonly rows = computed<readonly ListedVersion[]>(() => {
    const found = this.versionStore.found();
    return found === null ? [] : [...found.exact, ...found.throughStudy];
  });

  /** The detail of one version: `undefined` while it is read (a link is never offered on an older answer), `null` if it could not be. */
  protected detailOf(id: string): ReturnType<OptionsSetupVersions['detailOf']> {
    return this.versionStore.reading() ? undefined : this.versionStore.detailOf(id);
  }

  /** Said when the list was cut at its limit: "none" would then mean "none in the newest N". */
  protected readonly cut = computed(() => this.evidence.cuts().approvals);

  protected readonly gate = computed<WorkspaceStateContent | null>(() => {
    const read = this.evidence.state();
    if (read.kind === 'failed') {
      return {
        kind: 'failed',
        happened: 'The approved versions of this setup could not be read.',
        why: read.reason,
        next: 'No version can be chosen until they are read.',
        actions: [{ id: 'retry', label: 'Read them again' }],
      };
    }
    if (read.kind === 'loading') return { kind: 'checking', happened: 'Reading the approved versions of this setup.' };
    // The workspace opens only a setup the library holds; its content is not in hand for the moment after a save.
    if (this.store.savedHash() === null) return { kind: 'checking', happened: 'Reading the saved setup.' };
    return null;
  });

  constructor() {
    // Opening the choice reads what each version says now: a link is offered only for one that is ready at this moment.
    this.versionStore.reloadDetails();
  }

  protected versionQuery(id: string): Record<string, string> {
    return optionsDeployHandoffQuery(id);
  }

  protected retry(): void {
    this.evidence.reload();
    this.versionStore.reloadDetails();
  }
}
