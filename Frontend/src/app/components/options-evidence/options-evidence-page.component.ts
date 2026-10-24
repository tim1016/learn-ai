import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { OptionsSetupLibrary } from '../../options-setups/index/options-setup-library';
import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';
import { botReturnPath } from './bot-return';
import { OptionsEvidenceViewComponent } from './options-evidence-view.component';

/**
 * Retained Options evidence at its own address, `/options-setups/evidence/:kind/:subjectId`, with no saved name:
 * a run, a search study or an approved version opens by what it froze, whether or not a setup still holds that
 * content. The view is the evidence itself; the page is where it sits when no setup's Explore holds it.
 */
@Component({
  selector: 'app-options-evidence-page',
  imports: [BreadcrumbComponent, RouterLink, OptionsEvidenceViewComponent],
  // The saved setups, read once for the page, to say which of them hold the evidence's content.
  providers: [OptionsSetupLibrary],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-evidence-page.component.html',
  styleUrl: './options-evidence-page.component.scss',
})
export class OptionsEvidencePageComponent {
  readonly kind = input.required<string>();
  readonly subjectId = input.required<string>();
  readonly view = input<string>();
  readonly inspected_study_id = input<string>();
  readonly comparator_run_id = input<string>();
  readonly bot_return = input<string>();
  protected readonly botReturn = computed(() => botReturnPath(this.bot_return()));
}
