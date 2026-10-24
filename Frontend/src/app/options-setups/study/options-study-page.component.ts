import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { botReturnPath } from '../../components/options-evidence/bot-return';
import { OptionsEvidenceViewComponent } from '../../components/options-evidence/options-evidence-view.component';
import { OptionsStudyWorkbenchComponent } from '../../components/options-golden-search/options-study-workbench.component';
import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';
import { OptionsSetupLibrary } from '../index/options-setup-library';
import { optionsStudyRoute } from '../options-setups.address';

/**
 * One golden search study, at the address it has by its own id
 * (`/options-setups/evidence/study/:studyId`): the study to work on (its research, the
 * comparison and final test, and the approval), and the record of what it froze (its
 * research lineage). The study froze the content it started from, so it opens
 * whether or not a saved setup still holds that content, and the page says which do.
 * It is where an old Options Golden Search address for a study ends.
 */
@Component({
  selector: 'app-options-study-page',
  imports: [RouterLink, BreadcrumbComponent, OptionsStudyWorkbenchComponent, OptionsEvidenceViewComponent],
  // The saved setups, read once for the page (and only when the lineage view asks), to say which hold the study's content.
  providers: [OptionsSetupLibrary],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-study-page.component.html',
  styleUrl: './options-study-page.component.scss',
})
export class OptionsStudyPageComponent {
  readonly studyId = input.required<string>();
  /** `lineage` shows the study's recorded lineage; any other value, or none, shows the study to work on. */
  readonly view = input<string>();
  readonly inspected_study_id = input<string>();
  readonly comparator_run_id = input<string>();
  readonly bot_return = input<string>();

  protected readonly address = computed(() => optionsStudyRoute(this.studyId()));
  protected readonly botReturn = computed(() => botReturnPath(this.bot_return()));
  protected readonly lineage = computed(() => this.view() === 'lineage');
  /** What every link of the page keeps: the bot the owner came from, and the study another view inspected. */
  private readonly kept = computed(() => ({ inspected_study_id: this.inspected_study_id(), comparator_run_id: this.comparator_run_id(), bot_return: this.botReturn() }));
  protected readonly studyQuery = this.kept;
  protected readonly lineageQuery = computed(() => ({ ...this.kept(), view: 'lineage' }));
}
