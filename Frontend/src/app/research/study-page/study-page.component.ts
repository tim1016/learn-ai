import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { WorkspaceAction } from '../../shared/workspace/workspace-action';
import type { StudyPrefill } from '../stored-study/rerun';

import { HowThisWorksLinkComponent } from '../../shared/help/how-this-works-link.component';
import { menuLabelFor } from '../../shell/app-menu';
import type { DataSourceProvenance } from '../../shared/workspace/data-source-provenance';
import type { TimestampDisplayMode } from '../../shared/timestamp/timestamp-display';
import { PreviousResultComponent } from '../../shared/workspace/previous-result.component';
import type { ResultInputs } from '../../shared/workspace/previous-result';
import { WorkspaceDataSourceComponent } from '../../shared/workspace/workspace-data-source.component';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { studyDefinition, type ResearchStudyId } from '../research-studies';

const IDLE: WorkspaceStateContent = {
  kind: 'idle',
  happened: 'No result yet.',
  why: 'Nothing has run for the inputs above.',
  next: 'Choose the inputs, then press Run study.',
};

/**
 * One layout for every research study (PRD #3490 R24): the question, then the
 * instrument or universe and its window, the test configuration and Run study,
 * then the result with its limitations, its next step and the data-source line.
 *
 * Hosts project their own pickers and result into the slots; the page owns the
 * order and what is fixed:
 *   - `[studyInstrument]` the shared picker for the instrument or universe and window.
 *   - `[studyConfig]`     the test's own configuration.
 *   - `[studyProgress]`   a running job's progress and any error, drawn before the result.
 *   - `[studyResult]`     the finished result. It leads with the server's own
 *     assessment where the study has one; a descriptive study has none to lead with.
 *
 * What a study fixes (its session, its bar size) is read from the registry and
 * drawn as a label beside step 1, so no control is offered that the study
 * ignores. Presentation only: it never starts a run. Run study emits `run`
 * and the host's `RunGate` decides whether that press starts one.
 */
@Component({
  selector: 'app-study-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    HowThisWorksLinkComponent,
    PreviousResultComponent,
    RouterLink,
    TimestampDisplayComponent,
    WorkspaceDataSourceComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './study-page.component.html',
  styleUrl: './study-page.component.scss',
})
export class StudyPageComponent {
  readonly study = input.required<ResearchStudyId>();
  /** What step 3 says is about to run: "RSI(14) on SPY, 2025-10-01 to 2026-10-01". */
  readonly readySummary = input.required<string>();
  /** The inputs are complete and nothing is already running. */
  readonly canRun = input.required<boolean>();
  /** A press has been accepted and its run has not finished. */
  readonly running = input(false);
  /** Why Run study cannot start yet, in the host's words. */
  readonly blockedReason = input<string | null>(null);
  /** Anything beyond the idle state is on screen: a running job, an error or a result. */
  readonly hasOutput = input(false);
  /** A finished result is on screen; the study's limitations and next step go with it. */
  readonly hasResult = input(false);
  /** The shown result answers inputs other than the ones now entered. */
  readonly previous = input(false);
  readonly captured = input<ResultInputs | null>(null);
  /**
   * The server's provenance for the shown result. Left unset, the line names
   * the study's provider as it reads today; a study that moves to the lake
   * hands over what its result records.
   */
  readonly dataSource = input<DataSourceProvenance | null | undefined>(undefined);
  /**
   * How the data-source line writes its window. A study over a lake window hands over dates (ET midnights), which
   * read as `date-et`; the default writes the exchange-aligned instant.
   */
  readonly dataSourceWindows = input<Extract<TimestampDisplayMode, 'et' | 'date-et'>>('et');
  /** The saved run the inputs were filled in from (`?rerun=`), or why they were not. */
  readonly prefill = input<StudyPrefill | null>(null);

  /** One press of Run study. */
  readonly run = output();

  protected readonly definition = computed(() => studyDefinition(this.study()));
  protected readonly title = computed(() => menuLabelFor(this.definition().route));
  protected readonly instrumentHeading = computed(() =>
    this.definition().subject === 'universe' ? 'Universe and window' : 'Instrument and window',
  );
  protected readonly provenance = computed(() => {
    const own = this.dataSource();
    return own === undefined ? this.definition().source : own;
  });
  /** Every run is saved, so every study offers the list of its own. */
  protected readonly savedRuns = computed<readonly WorkspaceAction[]>(() => [
    {
      id: 'saved-runs',
      label: 'Saved runs',
      route: ['/research/history'],
      queryParams: { type: this.definition().history },
    },
  ]);
  protected readonly descriptive = computed(() => this.definition().resultKind === 'descriptive');
  protected readonly idle = IDLE;

  protected press(): void {
    this.run.emit();
  }
}
