import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { extractServerMessage } from '../../components/broker/operation-error';
import { OptionsBacktestService, SetupRefusedError } from '../../components/options-backtest/options-backtest.service';
import { AssetIdentityComponent } from '../../shared/asset-identity';
import { HowThisWorksLinkComponent } from '../../shared/help/how-this-works-link.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { ConfirmDeleteComponent } from '../../shared/research-record/confirm-delete.component';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { WorkspaceAction } from '../../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { optionsAnalysisRoute, optionsEvidenceRoute, optionsSetupRoute, type OptionsEvidenceKind } from '../options-setups.address';
import { evidenceHolding, holdersOf, newestFirst, type ContentHolders } from './options-setup-evidence';
import { OptionsSetupIndexService, type IndexedSetup, type OptionsSetupIndex } from './options-setup-index.service';

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
  readonly holders: ContentHolders;
  readonly route: string[];
}

/** What a saved setup has recorded for exactly its content. */
interface SetupRow {
  readonly setup: IndexedSetup;
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
  templateUrl: './options-setups-index.component.html',
  styleUrl: './options-setups-index.component.scss',
})
export class OptionsSetupsIndexComponent {
  private readonly indexService = inject(OptionsSetupIndexService);
  private readonly backtest = inject(OptionsBacktestService);

  protected readonly index = resource({ loader: () => this.indexService.read() });
  protected readonly filters = FILTERS;
  protected readonly filter = signal<KindFilter>('all');
  private readonly limit = signal(PAGE);
  protected readonly deleteProblem = signal<string | null>(null);

  protected readonly newSetup: WorkspaceAction = { id: 'new', label: 'New options setup', route: optionsSetupRoute(null, 'define') };
  /** Fixed-leg payoff analysis. It is named for what it is, so it is not taken for a way to start a setup. */
  protected readonly analysis: WorkspaceAction = { id: 'analysis', label: 'Strategy Builder (analysis only)', route: optionsAnalysisRoute('builder') };
  protected readonly setupRoute = optionsSetupRoute;

  protected readonly loaded = computed<OptionsSetupIndex | null>(() => (this.index.hasValue() ? this.index.value() : null));

  protected readonly state = computed<WorkspaceStateContent | null>(() => {
    if (this.index.error() !== undefined) {
      return {
        kind: 'failed',
        happened: 'The saved setups and their evidence could not be read.',
        why: extractServerMessage(this.index.error(), 'The research store did not answer.'),
        next: 'Read them again. Nothing stored is changed by a failed read.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    return this.loaded() === null ? { kind: 'checking', happened: 'Reading the saved setups and their evidence.' } : null;
  });

  protected readonly setups = computed<readonly SetupRow[]>(() => {
    const index = this.loaded();
    if (index === null) return [];
    return index.setups.map((setup) => {
      const held = evidenceHolding(index, setup.setup_sha256);
      const latest = newestFirst(held.approvals)[0];
      return {
        setup,
        runs: held.runs.length,
        studies: held.studies.length,
        approvals: held.approvals.length,
        latestApproval: latest === undefined ? null : latest.status,
        sharedWith:
          setup.setup_sha256 === null
            ? []
            : index.setups.filter((other) => other.name !== setup.name && other.setup_sha256 === setup.setup_sha256).map((other) => other.name),
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
      holders: holdersOf(index, run),
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
      holders: holdersOf(index, study),
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
      holders: holdersOf(index, version),
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
    if (id === 'retry') this.index.reload();
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
    this.index.reload();
  }
}
