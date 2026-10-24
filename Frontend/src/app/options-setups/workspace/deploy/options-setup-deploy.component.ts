import { ChangeDetectionStrategy, Component, computed, inject, output, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { extractServerMessage } from '../../../components/broker/operation-error';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../../components/options-backtest/golden-option-setups.service';
import { GOLDEN_DEPLOY_HANDOFF_ROUTE } from '../../../fleet/account-workspace';
import { optionsDeployHandoffQuery } from '../../../fleet/options-setup-handoff';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { OptionsSetupIndexService } from '../../index/options-setup-index.service';
import { optionsEvidenceRoute, optionsSetupRoute } from '../../options-setups.address';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { PATH_TITLE, readVersionDetails, versionsOfSetup, type ListedVersion } from '../golden/golden-versions';

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
 * Deploy re-checks the version, the account and its gates at its own boundary.
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
  private readonly indexService = inject(OptionsSetupIndexService);
  private readonly goldenService = inject(GoldenOptionSetupsService);

  readonly closed = output();

  protected readonly accountListRoute = GOLDEN_DEPLOY_HANDOFF_ROUTE;
  protected readonly evidenceRoute = optionsEvidenceRoute;
  protected readonly pathTitle = PATH_TITLE;
  protected readonly goldenRoute = computed(() => optionsSetupRoute(this.store.address(), 'golden'));
  /** Versions are of the content as last saved: an edit in Define that is not saved changes none of them. */
  protected readonly draftChanged = computed(() => this.store.standing().kind === 'changed');

  private readonly index = resource({ loader: () => this.indexService.read() });

  private readonly saved = computed(() => {
    const name = this.store.address();
    if (name === null || !this.index.hasValue()) return null;
    return this.index.value().setups.find((setup) => setup.name === name) ?? null;
  });

  /** The approved versions of the saved content, then those a search that began from it made with changed settings. */
  protected readonly rows = computed<readonly ListedVersion[]>(() => {
    if (!this.index.hasValue()) return [];
    const found = versionsOfSetup(this.index.value(), this.saved()?.setup_sha256);
    return [...found.exact, ...found.throughStudy];
  });
  private readonly ids = computed(() => this.rows().map((version) => version.approval.id));

  /** Each version read by its own id: its status now, and why when it is not ready. */
  private readonly details = resource({
    params: () => (this.ids().length === 0 ? undefined : { ids: this.ids() }),
    loader: ({ params }) => readVersionDetails(this.goldenService, params.ids),
  });

  /** The detail of one version: `undefined` while it is read, `null` if it could not be. */
  protected detailOf(id: string): GoldenOptionSetup | null | undefined {
    if (this.details.hasValue()) return this.details.value().get(id) ?? null;
    return this.details.error() === undefined ? undefined : null;
  }

  /** Said when the list was cut at its limit: "none" would then mean "none in the newest N". */
  protected readonly cut = computed(() => {
    if (!this.index.hasValue()) return null;
    const index = this.index.value();
    return index.approvals_total > index.approvals.length ? `the newest ${index.approvals.length} of ${index.approvals_total}` : null;
  });

  protected readonly gate = computed<WorkspaceStateContent | null>(() => {
    if (this.index.error() !== undefined) {
      return {
        kind: 'failed',
        happened: 'The approved versions of this setup could not be read.',
        why: extractServerMessage(this.index.error(), 'The research store did not answer.'),
        next: 'No version can be chosen until they are read.',
        actions: [{ id: 'retry', label: 'Read them again' }],
      };
    }
    if (this.index.isLoading() && !this.index.hasValue()) return { kind: 'checking', happened: 'Reading the approved versions of this setup.' };
    if (this.saved() === null) {
      return {
        kind: 'unavailable',
        happened: 'The saved setup was not in the evidence read.',
        why: 'It may have been saved or removed a moment ago.',
        next: 'Read it again. No version is chosen in its place.',
        actions: [{ id: 'retry', label: 'Read it again' }],
      };
    }
    return null;
  });

  protected versionQuery(id: string): Record<string, string> {
    return optionsDeployHandoffQuery(id);
  }

  protected retry(): void {
    this.index.reload();
    this.details.reload();
  }
}
