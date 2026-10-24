import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { extractServerMessage } from '../../../components/broker/operation-error';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../../components/options-backtest/golden-option-setups.service';
import { GoldenSetupApprovalComponent } from '../../../components/options-backtest/run/golden-setup-approval.component';
import { OptionsEvidenceService } from '../../../components/options-evidence/options-evidence.service';
import { OptionsSearchPlanComponent } from '../../../components/options-golden-search/options-search-plan.component';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { OptionsSetupIndexService } from '../../index/options-setup-index.service';
import { optionsEvidenceRoute, optionsSetupRoute } from '../../options-setups.address';
import { describeStanding } from '../draft-standing';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { OptionsGoldenVersionComponent } from './golden-version.component';
import { reviewableRuns, reviewedRunId, versionsOfSetup } from './golden-versions';

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
 * saved name holds is found by exact content in the evidence index, never by the name.
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
  private readonly indexService = inject(OptionsSetupIndexService);
  private readonly goldenService = inject(GoldenOptionSetupsService);
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

  protected readonly index = resource({ loader: () => this.indexService.read() });

  /** The saved setup, as the index read it just now. */
  private readonly saved = computed(() => {
    const name = this.name();
    if (name === null || !this.index.hasValue()) return null;
    return this.index.value().setups.find((setup) => setup.name === name) ?? null;
  });
  protected readonly hash = computed(() => this.saved()?.setup_sha256 ?? null);

  // The versions.
  protected readonly versions = computed(() => (this.index.hasValue() ? versionsOfSetup(this.index.value(), this.hash()) : null));
  private readonly versionIds = computed(() => {
    const found = this.versions();
    return found === null ? [] : [...found.exact, ...found.throughStudy].map((listed) => listed.approval.id);
  });
  /** Each version read by its own id, so none is lost to the newest-first limit of a list. A version that cannot be read says so. */
  private readonly detailsRead = resource({
    params: () => (this.versionIds().length === 0 ? undefined : { ids: this.versionIds() }),
    loader: async ({ params }) => {
      const settled = await Promise.allSettled(params.ids.map((id) => this.goldenService.get(id)));
      return new Map<string, GoldenOptionSetup | null>(params.ids.map((id, at) => {
        const outcome = settled[at];
        return [id, outcome.status === 'fulfilled' ? outcome.value : null];
      }));
    },
  });
  /** The detail of one version: `undefined` while it is read, `null` if it could not be. */
  protected readonly detailOf = (id: string): GoldenOptionSetup | null | undefined =>
    this.detailsRead.hasValue() ? (this.detailsRead.value().get(id) ?? null) : this.detailsRead.error() === undefined ? undefined : null;
  /** Said when a list was cut at its limit: "none" would then mean "none in the newest N". */
  protected readonly approvalsCut = computed(() => {
    if (!this.index.hasValue()) return null;
    const index = this.index.value();
    return index.approvals_total > index.approvals.length ? `the newest ${index.approvals.length} of ${index.approvals_total}` : null;
  });
  protected readonly studiesCut = computed(() => {
    if (!this.index.hasValue()) return null;
    const index = this.index.value();
    return index.studies_total > index.studies.length ? `the newest ${index.studies.length} of ${index.studies_total}` : null;
  });
  protected readonly runsCut = computed(() => {
    if (!this.index.hasValue()) return null;
    const index = this.index.value();
    return index.runs_total > index.runs.length ? `the newest ${index.runs.length} of ${index.runs_total}` : null;
  });

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
  protected readonly runs = computed(() => (this.index.hasValue() ? reviewableRuns(this.index.value(), this.hash()) : []));
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
    if (this.index.error() !== undefined) {
      return {
        kind: 'failed',
        happened: 'The evidence recorded for this setup could not be read.',
        why: extractServerMessage(this.index.error(), 'The research store did not answer.'),
        next: 'Read it again. Nothing about the setup itself is lost.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    if (this.index.isLoading() && !this.index.hasValue()) return { kind: 'checking', happened: 'Reading the evidence recorded for this setup.' };
    if (this.saved() === null) {
      return {
        kind: 'unavailable',
        happened: 'The saved setup was not in the evidence read.',
        why: 'It may have been saved or removed a moment ago.',
        next: 'Read it again.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    return null;
  });

  protected act(id: string): void {
    if (id === 'retry') this.index.reload();
  }

  /** A version was approved, revoked or re-proved: what is listed is read again. */
  protected versionChanged(): void {
    this.index.reload();
    this.detailsRead.reload();
  }

  protected togglePlanning(): void {
    this.planning.update((open) => !open);
  }
}
