import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { extractServerMessage } from '../../../components/broker/operation-error';
import { GoldenSetupApprovalComponent } from '../../../components/options-backtest/run/golden-setup-approval.component';
import { OptionsEvidenceService } from '../../../components/options-evidence/options-evidence.service';
import { OptionsSearchPlanComponent } from '../../../components/options-golden-search/options-search-plan.component';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { OptionsSetupEvidenceStore } from '../../index/options-setup-evidence-store';
import { optionsEvidenceRoute, optionsSetupRoute } from '../../options-setups.address';
import { describeStanding } from '../draft-standing';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { OptionsSetupVersions } from '../options-setup-versions';
import { OptionsGoldenVersionComponent } from './golden-version.component';
import { reviewableRuns, reviewedRunId } from './golden-versions';

/** How many of the setup's runs are offered for review before the rest are left to the Options setups list. */
const RUNS_OFFERED = 8;

/**
 * Golden: where an options setup's tested runs and searches become approved versions (PRD #3490 R22).
 *
 * Both ways to a version are here, and both publish through the existing Golden Options authority:
 * **approve a tested run** (a note, the run's own proof) and **start a golden search** from the saved
 * content. Each version says which of the two made it, what it came from, whether its proof holds, what
 * limits it and what the owner excepted, and what was revoked or re-proved since.
 *
 * Nothing here reads the draft in Define. A review is of the run named in the address (`?run=`), by what that
 * run froze, and an approval carries the hash of that content, so an edit made elsewhere in the meantime
 * cannot change what is approved. A search starts from the content as last saved, named by its hash. What a
 * saved name holds is found by exact content in the evidence the workspace holds, never by the name; that evidence
 * and each version read by its id are read once for the workspace, so moving between tabs asks for neither again.
 */
@Component({
  selector: 'app-options-setup-golden',
  imports: [
    RouterLink,
    AuthoredUsdPipe,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
    WorkspaceStateBlockComponent,
    GoldenSetupApprovalComponent,
    OptionsGoldenVersionComponent,
    OptionsSearchPlanComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-setup-golden.component.html',
  styleUrl: './options-setup-golden.component.scss',
})
export class OptionsSetupGoldenComponent {
  private readonly store = inject(OptionsSetupWorkspaceStore);
  private readonly evidence = inject(OptionsSetupEvidenceStore);
  private readonly versionStore = inject(OptionsSetupVersions);
  private readonly evidenceService = inject(OptionsEvidenceService);

  /** The run the review is of (`?run=`). Taking a run to Golden carries this and nothing else. */
  readonly run = input<string | undefined>(undefined);
  /** The study a new plan forks (`?parent_study_id=`). */
  readonly parent_study_id = input<string | undefined>(undefined);

  protected readonly name = this.store.address;
  /** The saved name, for the parts that exist only when there is one: a draft with no name shows the state that says so instead. */
  protected readonly savedName = computed(() => this.name() ?? '');
  protected readonly standing = computed(() => describeStanding(this.store.standing()));
  protected readonly draftChanged = computed(() => this.store.standing().kind === 'changed');
  protected readonly evidenceRoute = optionsEvidenceRoute;
  protected readonly defineRoute = computed(() => optionsSetupRoute(this.name(), 'define'));
  protected readonly goldenRoute = computed(() => optionsSetupRoute(this.name(), 'golden'));

  /** The content the setup is saved as, from the library: what everything below is found by. */
  protected readonly hash = this.store.savedHash;

  // The versions.
  protected readonly versions = this.versionStore.found;
  /** The detail of one version: `undefined` while it is read, `null` if it could not be. */
  protected readonly detailOf = (id: string) => this.versionStore.detailOf(id);
  /** Said where a list was cut at its limit: "none" would then mean "none in the newest N". */
  protected readonly cuts = this.evidence.cuts;

  // The review of one tested run.
  protected readonly reviewed = computed(() => reviewedRunId(this.run()));
  protected readonly runNamesNothing = computed(() => this.run() !== undefined && this.reviewed() === null);
  private readonly reviewRead = resource({
    params: () => this.reviewed() ?? undefined,
    loader: ({ params }) => this.evidenceService.read('run', String(params)),
  });
  /** The run's own record of what it froze, once read for the run under review and no other. */
  protected readonly frozen = computed(() => {
    const id = this.reviewed();
    const read = this.reviewRead.hasValue() ? this.reviewRead.value() : null;
    return read !== null && id !== null && read.subject_id === String(id) ? read : null;
  });
  protected readonly reviewProblem = computed(() =>
    this.reviewRead.error() === undefined ? null : extractServerMessage(this.reviewRead.error(), `Run ${this.reviewed()} could not be read.`),
  );
  /** Whether what the run froze is what this setup is saved as now. It is not required: a review is of the run. */
  protected readonly frozenIsSaved = computed(() => {
    const frozen = this.frozen();
    const hash = this.hash();
    return frozen === null || hash === null ? null : frozen.setup_sha256 === hash;
  });
  protected readonly runs = computed(() => {
    const index = this.evidence.index();
    return index === null ? [] : reviewableRuns(index, this.hash());
  });
  protected readonly runsOffered = computed(() => this.runs().slice(0, RUNS_OFFERED));
  protected readonly reviewIds = computed(() => {
    const id = this.reviewed();
    return id === null ? [] : [id];
  });

  // The searches.
  protected readonly studies = computed(() => this.versions()?.studies ?? []);
  /** The plan opens at once when it forks a study, and otherwise when the owner asks. */
  protected readonly planning = linkedSignal<string | undefined, boolean>({ source: this.parent_study_id, computation: (parent) => parent !== undefined });

  protected readonly state = computed<WorkspaceStateContent | null>(() => {
    if (this.name() === null) {
      return {
        kind: 'idle',
        happened: 'This draft has no saved name, so no saved content is here to approve a run of or to search from.',
        next: 'Save it under a name in Define. Golden then works on exactly the content you saved.',
        actions: [{ id: 'define', label: 'Go to Define', route: this.defineRoute() }],
      };
    }
    const read = this.evidence.state();
    if (read.kind === 'failed') {
      return {
        kind: 'failed',
        happened: 'The evidence recorded for this setup could not be read.',
        why: read.reason,
        next: 'Read it again. Nothing about the setup itself is lost.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    if (read.kind === 'loading') return { kind: 'checking', happened: 'Reading the evidence recorded for this setup.' };
    // The workspace opens only a setup the library holds; its content is not in hand for the moment after a save.
    if (this.hash() === null) return { kind: 'checking', happened: 'Reading the saved setup.' };
    return null;
  });

  protected act(id: string): void {
    if (id === 'retry') this.evidence.reload();
  }

  /** A version was approved, revoked or re-proved: what is listed is read again. */
  protected versionChanged(): void {
    this.evidence.reloadIndex();
    this.versionStore.reloadDetails();
  }

  protected togglePlanning(): void {
    this.planning.update((open) => !open);
  }
}
