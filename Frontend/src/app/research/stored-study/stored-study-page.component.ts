import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { IndicatorReliabilityReportComponent } from '../../components/research-lab/indicator-reliability/indicator-reliability-report/indicator-reliability-report.component';
import { CrossSectionalReportComponent } from '../../components/research-lab/batch-runner/cross-sectional-report/cross-sectional-report.component';
import { FeatureReportComponent } from '../../components/research-lab/feature-report/feature-report.component';
import { SignalReportComponent } from '../../components/research-lab/signal-report/signal-report.component';
import { RealizedVsIvReportComponent } from '../../components/edge/realized-vs-iv/realized-vs-iv-report.component';
import { menuLabelFor } from '../../shell/app-menu';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { WorkspaceAction } from '../../shared/workspace/workspace-action';
import { WorkspaceDataSourceComponent } from '../../shared/workspace/workspace-data-source.component';
import { WorkspaceHeaderComponent, type WorkspaceBreadcrumbItem } from '../../shared/workspace/workspace-header.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { RESEARCH_STUDIES } from '../research-studies';
import { StoredInputsError } from './stored-inputs';
import {
  StoredStudyNotFoundError,
  StoredStudyService,
  StoredStudyUnreadableError,
  type StoredStudyRecord,
  type StoredStudyType,
} from './stored-study';
import { storedDataSource } from './stored-study-data-source';
import { storedFacts, type StoredFact } from './stored-study-facts';
import { storedStudyView } from './stored-study-view';

const RETRY = [{ id: 'retry', label: 'Try again' }] as const;

const STATUS_WORDS: Readonly<Record<StoredStudyRecord['status'], string>> = {
  completed: 'Completed',
  partial: 'Partial',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

/**
 * A saved study run (PRD #3490 R26): what was asked, how the run ended and, when it left one, the result, drawn with
 * the view the study's own page draws a live result with.
 *
 * Opening it reads the record the data plane stored when the run ended. It starts no job, fetches no bars and
 * computes nothing, so a reload shows the same figures. Rerun is the one other thing the page offers, and it only
 * opens the study's page with these inputs filled in; the run is that page's Run study, pressed by the reader.
 *
 * Read-only: the report components take their result as an input and hold no control that changes it.
 */
@Component({
  selector: 'app-stored-study-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CrossSectionalReportComponent,
    FeatureReportComponent,
    IndicatorReliabilityReportComponent,
    RealizedVsIvReportComponent,
    RouterLink,
    SignalReportComponent,
    TimestampDisplayComponent,
    WorkspaceDataSourceComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './stored-study-page.component.html',
  styleUrl: './stored-study-page.component.scss',
})
export class StoredStudyPageComponent {
  /** The History kind, from the route's data. */
  readonly study = input.required<StoredStudyType>();
  /** The run's id, from the route. */
  readonly id = input.required<string>();

  private readonly service = inject(StoredStudyService);

  protected readonly stored = resource({
    params: () => ({ study: this.study(), id: this.id() }),
    loader: ({ params }) => this.service.read(params.study, params.id),
  });

  protected readonly record = computed(() => (this.stored.hasValue() ? this.stored.value() : null));

  private readonly definition = computed(() => {
    const found = RESEARCH_STUDIES.find((candidate) => candidate.history === this.study());
    if (found === undefined) throw new Error(`No research study keeps ${this.study()} runs.`);
    return found;
  });

  protected readonly label = computed(() => menuLabelFor(this.definition().route));

  protected readonly title = computed(() => `${this.label()}, saved run`);

  protected readonly breadcrumb = computed<readonly WorkspaceBreadcrumbItem[]>(() => [
    { label: 'Research', route: '/research' },
    { label: 'History', route: ['/research/history'], queryParams: { type: this.study() } },
    { label: this.label() },
  ]);

  protected readonly historyLink = computed(() => ({ route: ['/research/history'], queryParams: { type: this.study() } }));

  /** Rerun opens the study's page filled in from this run. It starts nothing. */
  protected readonly rerun = computed<WorkspaceAction | null>(() => {
    const saved = this.record();
    return saved === null
      ? null
      : {
          id: 'rerun',
          label: 'Rerun with these inputs',
          route: [this.definition().route],
          queryParams: { rerun: saved.record_id },
        };
  });

  protected readonly statusWords = computed(() => {
    const saved = this.record();
    return saved === null ? '' : STATUS_WORDS[saved.status];
  });

  protected readonly facts = computed<{ readonly list: readonly StoredFact[]; readonly unreadable: boolean }>(() => {
    const saved = this.record();
    if (saved === null) return { list: [], unreadable: false };
    try {
      return { list: storedFacts(saved), unreadable: false };
    } catch (error) {
      if (error instanceof StoredInputsError) return { list: [], unreadable: true };
      throw error;
    }
  });

  protected readonly view = computed(() => {
    const saved = this.record();
    return saved === null ? null : storedStudyView(saved);
  });

  protected readonly provenance = computed(() => {
    const saved = this.record();
    return saved === null ? null : storedDataSource(saved);
  });

  protected readonly parent = computed(() => {
    const parent = this.record()?.rerun_of ?? null;
    return parent === null ? null : { label: parent.native_id.slice(0, 8), link: ['/research/history', parent.type, parent.native_id] };
  });

  /** The words for a run that did not end cleanly; `null` for one that did. */
  protected readonly ending = computed<WorkspaceStateContent | null>(() => {
    const saved = this.record();
    if (saved === null) return null;
    const why = saved.status_detail;
    switch (saved.status) {
      case 'failed':
        return {
          kind: 'failed',
          happened: 'This run failed.',
          why,
          next: 'It left no result. Rerun it as it was, or change the inputs first.',
        };
      case 'cancelled':
        return {
          kind: 'cancelled',
          happened: 'This run was cancelled before it finished.',
          why,
          next: 'It left no result. Rerun it to get one.',
        };
      case 'partial':
        return {
          kind: 'partial',
          happened: 'This run finished with part of its work missing.',
          why,
          next: 'The result below covers only what finished.',
        };
      case 'completed':
        return null;
    }
  });

  /** The page-level state when the run could not be opened. */
  protected readonly unopened = computed<WorkspaceStateContent | null>(() => {
    const error = this.stored.error();
    if (error === undefined) return null;
    if (error instanceof StoredStudyNotFoundError) {
      return {
        kind: 'unavailable',
        happened: 'This run is not saved.',
        why: `No ${this.label()} run is saved under this id.`,
        next: 'Open the History to find the run you meant.',
        actions: [
          {
            id: 'history',
            label: 'Back to History',
            route: ['/research/history'],
            queryParams: { type: this.study() },
          },
        ],
      };
    }
    if (error instanceof StoredStudyUnreadableError) {
      return {
        kind: 'failed',
        happened: 'This saved run could not be read.',
        why: error.reason,
        next: 'Nothing was changed. The run stays in the History as it was written.',
        actions: [{ id: 'history', label: 'Back to History', route: ['/research/history'], queryParams: { type: this.study() } }],
      };
    }
    return {
      kind: 'failed',
      happened: 'This saved run could not be opened.',
      why: 'The data plane did not answer.',
      next: 'Nothing was lost. Try again.',
      actions: RETRY,
    };
  });

  protected retry(): void {
    this.stored.reload();
  }
}
