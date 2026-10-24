import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { extractServerMessage } from '../../components/broker/operation-error';
import { OptionsBacktestService, SetupRefusedError } from '../../components/options-backtest/options-backtest.service';
import type { SavedSetup } from '../../components/options-backtest/options-backtest.types';
import { AssetIdentityComponent } from '../../shared/asset-identity';
import { HowThisWorksLinkComponent } from '../../shared/help/how-this-works-link.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { ConfirmDeleteComponent } from '../../shared/research-record/confirm-delete.component';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { WorkspaceAction } from '../../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { GOLDEN_PARENT_STUDY_KEY, OPTIONS_CHOOSE_GOLDEN, optionsAnalysisRoute, optionsEvidenceRoute, optionsSetupRoute, type OptionsEvidenceKind, type OptionsSetupTab } from '../options-setups.address';
import { namesHolding, newestFirst, type ContentHolders } from './options-setup-evidence';
import { OptionsSetupEvidenceStore } from './options-setup-evidence-store';
import type { OptionsSetupIndex } from './options-setup-index.service';
import { OptionsSetupLibrary } from './options-setup-library';

type KindFilter = 'all' | OptionsEvidenceKind;

/** One piece of evidence in the list, whichever kind it is. */
interface EvidenceRow {
  readonly kind: OptionsEvidenceKind;
  readonly id: string;
  /** What to call it: "Run 76", or the study's or version's id. */
  readonly title: string;
  /** The label it was recorded under. Only a label: it says nothing of who holds the content now. */
  readonly recordedName: string | null;
  readonly underlying: string | null;
  readonly createdAtMs: number;
  readonly status: string;
  readonly detail: string | null;
  readonly holders: ContentHolders<SavedSetup>;
  readonly route: string[];
}

/** What a saved setup has recorded for exactly its content. */
interface SetupRow {
  readonly setup: SavedSetup;
  /** Whether it can be opened: a setup that cannot be has no content hash, and so no page to open. */
  readonly openable: boolean;
  readonly underlying: string | null;
  readonly runs: number;
  readonly studies: number;
  readonly approvals: number;
  readonly latestApproval: string | null;
  readonly sharedWith: readonly string[];
}

const PAGE = 100;

const FILTERS: readonly { readonly id: KindFilter; readonly label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'run', label: 'Runs' },
  { id: 'study', label: 'Searches' },
  { id: 'golden', label: 'Approved versions' },
];

/**
 * Options setups (PRD #3490 R20, I08): the saved setups by name, and beside
 * them every run, search and approved version, whether or not a saved setup
 * still holds the content it froze. A name addresses a draft that can be edited,
 * deleted and reused, so evidence is never listed under one: each row opens by
 * its own id, and says which saved names hold its exact content today, if any.
 *
 * The saved setups and the evidence are two reads, each made once for the page and joined here by equal content.
 *
 * `?then=golden` asks which setup to continue with: an old Options Golden Search link that named no
 * setup ends here, and every saved setup then opens on its Golden tab, carrying the study it forks.
 */
@Component({
  selector: 'app-options-setups-index',
  imports: [
    RouterLink,
    AssetIdentityComponent,
    HowThisWorksLinkComponent,
    ReceiptLabelPipe,
    ConfirmDeleteComponent,
    TimestampDisplayComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [OptionsSetupLibrary, OptionsSetupEvidenceStore],
  templateUrl: './options-setups-index.component.html',
  styleUrl: './options-setups-index.component.scss',
})
export class OptionsSetupsIndexComponent {
  private readonly library = inject(OptionsSetupLibrary);
  private readonly store = inject(OptionsSetupEvidenceStore);
  private readonly backtest = inject(OptionsBacktestService);

  /** What the list is asked to continue with: `golden` sends each saved setup to its Golden tab. */
  readonly then = input<string | undefined>(undefined);
  /** The study a plan forks, kept for the Golden tab the owner chooses. */
  readonly parent_study_id = input<string | undefined>(undefined);
  protected readonly choosing = computed(() => this.then() === OPTIONS_CHOOSE_GOLDEN);
  /** The tab a saved setup opens on, and what it carries there. */
  protected readonly landing = computed<OptionsSetupTab>(() => (this.choosing() ? 'golden' : 'overview'));
  protected readonly landingQuery = computed(() => (this.choosing() ? { [GOLDEN_PARENT_STUDY_KEY]: this.parent_study_id() ?? null } : {}));

  protected readonly filters = FILTERS;
  protected readonly filter = signal<KindFilter>('all');
  private readonly limit = signal(PAGE);
  protected readonly deleteProblem = signal<string | null>(null);

  protected readonly newSetup: WorkspaceAction = { id: 'new', label: 'New options setup', route: optionsSetupRoute(null, 'define') };
  /** Fixed-leg payoff analysis. It is named for what it is, so it is not taken for a way to start a setup. */
  protected readonly analysis: WorkspaceAction = { id: 'analysis', label: 'Strategy Builder (analysis only)', route: optionsAnalysisRoute('builder') };
  protected readonly setupRoute = optionsSetupRoute;

  /** The evidence once it is read, and only once the saved setups it is joined to are read too. */
  protected readonly loaded = computed<OptionsSetupIndex | null>(() => (this.library.status() === 'ready' ? this.store.index() : null));

  protected readonly state = computed<WorkspaceStateContent | null>(() => {
    const evidence = this.store.state();
    if (this.library.status() === 'error' || evidence.kind === 'failed') {
      return {
        kind: 'failed',
        happened: 'The saved setups and their evidence could not be read.',
        why: this.library.problem() ?? (evidence.kind === 'failed' ? evidence.reason : 'The research store did not answer.'),
        next: 'Read them again. Nothing stored is changed by a failed read.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    return this.loaded() === null ? { kind: 'checking', happened: 'Reading the saved setups and their evidence.' } : null;
  });

  protected readonly setups = computed<readonly SetupRow[]>(() => {
    if (this.loaded() === null) return [];
    const saved = this.library.setups();
    return saved.map((setup) => {
      const held = this.store.evidenceHolding(setup.setup_sha256);
      const latest = newestFirst(held.approvals)[0];
      return {
        setup,
        openable: setup.setup_sha256 !== null && setup.setup_sha256 !== undefined,
        underlying: setup.setup?.underlying ?? null,
        runs: held.runs.length,
        studies: held.studies.length,
        approvals: held.approvals.length,
        latestApproval: latest === undefined ? null : latest.status,
        sharedWith: namesHolding(saved, setup.setup_sha256)
          .filter((other) => other.name !== setup.name)
          .map((other) => other.name),
      };
    });
  });

  private readonly evidence = computed<readonly EvidenceRow[]>(() => {
    const index = this.loaded();
    if (index === null) return [];
    const runs = index.runs.map<EvidenceRow>((run) => ({
      kind: 'run',
      id: String(run.id),
      title: `Run ${run.id}`,
      recordedName: run.recorded_name ?? null,
      underlying: run.underlying,
      createdAtMs: run.created_at_ms,
      status: run.status,
      detail: run.net_profit_usd === null || run.net_profit_usd === undefined ? null : `net ${run.net_profit_usd} USD`,
      holders: this.library.holdersOf(run),
      route: optionsEvidenceRoute('run', run.id),
    }));
    const studies = index.studies.map<EvidenceRow>((study) => ({
      kind: 'study',
      id: study.id,
      title: `Search ${study.id}`,
      recordedName: study.recorded_name,
      underlying: study.underlying,
      createdAtMs: study.created_at_ms,
      status: study.status,
      detail: `phase ${study.phase}`,
      holders: this.library.holdersOf(study),
      route: optionsEvidenceRoute('study', study.id),
    }));
    const versions = index.approvals.map<EvidenceRow>((version) => ({
      kind: 'golden',
      id: version.id,
      title: `Approved version ${version.id}`,
      recordedName: version.recorded_name,
      underlying: version.underlying ?? null,
      createdAtMs: version.created_at_ms,
      status: version.status,
      detail: version.origin === 'search' ? 'from a golden search' : version.origin === 'backtest' ? `from run ${version.source_run_id}` : 'origin not recorded',
      holders: this.library.holdersOf(version),
      route: optionsEvidenceRoute('golden', version.id),
    }));
    // Newest first; a tie goes by kind and id, so the order never depends on which list was read first.
    return [...runs, ...studies, ...versions].sort((a, b) => b.createdAtMs - a.createdAtMs || a.kind.localeCompare(b.kind) || a.id.localeCompare(b.id));
  });

  protected readonly visible = computed(() => {
    const kind = this.filter();
    return this.evidence().filter((row) => kind === 'all' || row.kind === kind);
  });
  protected readonly shown = computed(() => this.visible().slice(0, this.limit()));

  /** Each list says when it was cut at its limit, so a short list is never taken for a whole one. */
  protected readonly cuts = computed(() => {
    const index = this.loaded();
    if (index === null) return [];
    return [
      { what: 'runs', shown: index.runs.length, total: index.runs_total },
      { what: 'searches', shown: index.studies.length, total: index.studies_total },
      { what: 'approved versions', shown: index.approvals.length, total: index.approvals_total },
    ].filter((list) => list.total > list.shown);
  });

  protected act(id: string): void {
    if (id === 'retry') this.store.reload();
  }

  protected setFilter(kind: KindFilter): void {
    this.filter.set(kind);
    this.limit.set(PAGE);
  }

  protected showMore(): void {
    this.limit.update((limit) => limit + PAGE);
  }

  protected async remove(name: string): Promise<void> {
    this.deleteProblem.set(null);
    try {
      await this.backtest.remove(name);
    } catch (error) {
      this.deleteProblem.set(error instanceof SetupRefusedError ? error.message : extractServerMessage(error, 'The setup could not be deleted. Try again.'));
    }
    // A deleted name changes which names hold a content, and no evidence: only the saved setups are read again.
    this.library.reload();
  }
}
