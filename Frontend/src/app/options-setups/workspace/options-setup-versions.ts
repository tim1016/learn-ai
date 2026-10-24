import { computed, inject, Injectable, resource } from '@angular/core';

import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../components/options-backtest/golden-option-setups.service';
import { OptionsSetupEvidenceStore } from '../index/options-setup-evidence-store';
import { readVersionDetails, versionsOfSetup, type VersionsOfSetup } from './golden/golden-versions';
import { OptionsSetupWorkspaceStore } from './options-setup-store';

/**
 * The approved versions of the content a setup is saved as, and each read by its own id: the one place Golden and
 * the Deploy choice read them from (PRD #3490 R22, R23). A version is found by the exact content it froze, and,
 * apart from those, through a search that began from that content and changed its settings.
 *
 * A setup's workspace provides one, and it reads nothing until a tab that lists versions asks, then keeps what it
 * read while the owner moves between tabs. The list comes from the evidence the workspace already holds; the
 * details are read again when a version changes (`reloadDetails`) and when Deploy opens, because the Deploy choice
 * offers a link only for a version that is ready now.
 */
@Injectable()
export class OptionsSetupVersions {
  private readonly workspace = inject(OptionsSetupWorkspaceStore);
  private readonly evidence = inject(OptionsSetupEvidenceStore);
  private readonly golden = inject(GoldenOptionSetupsService);

  /** The versions of the saved content; null while the evidence is not read. */
  readonly found = computed<VersionsOfSetup | null>(() => {
    const index = this.evidence.index();
    return index === null ? null : versionsOfSetup(index, this.workspace.savedHash());
  });
  private readonly ids = computed(() => {
    const found = this.found();
    return found === null ? [] : [...found.exact, ...found.throughStudy].map((listed) => listed.approval.id);
  });

  /** Each version read by its own id, so none is lost to the newest-first limit of a list. */
  private readonly details = resource({
    params: () => (this.ids().length === 0 ? undefined : { ids: this.ids() }),
    loader: ({ params }) => readVersionDetails(this.golden, params.ids),
  });

  /** The detail of one version: `undefined` while it is read, `null` if it could not be. It keeps the last answer while a new one is read. */
  detailOf(id: string): GoldenOptionSetup | null | undefined {
    if (this.details.hasValue()) return this.details.value().get(id) ?? null;
    return this.details.error() === undefined ? undefined : null;
  }

  /** Whether the versions are being read now, so a caller that must not act on an answer older than this moment can wait. */
  readonly reading = computed(() => this.details.isLoading());

  /** A version was approved, revoked or re-proved, or Deploy is choosing among them: what each says now is read again. */
  reloadDetails(): void {
    this.details.reload();
  }
}
