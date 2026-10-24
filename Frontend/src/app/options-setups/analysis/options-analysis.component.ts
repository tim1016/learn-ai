import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { CurrentUrlService } from '../../shell/current-url.service';
import type { WorkspaceAction } from '../../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../../shared/workspace/workspace-tab-list.component';
import { OPTIONS_ANALYSIS_VIEWS, optionsAnalysisRoute, optionsAnalysisViewOf } from '../options-setups.address';

/**
 * Why the analysis cannot become a setup. A setup is replayed on a clock: it
 * says when to enter and exit and which strike and expiry to choose. Fixed
 * strikes, premiums and implied volatility say none of that, so no field of
 * this analysis can be carried into a setup without the owner inventing
 * rules (PRD #3490 R21, decision D04).
 */
export const ANALYSIS_NOT_A_SETUP_REASON =
  'A setup is replayed on a clock and needs entry and exit times and rules for choosing the strike and expiry. '
  + 'This analysis fixes strikes, premiums and implied volatility and has none, so it cannot be turned into a setup.';

const USE_AS_SETUP: WorkspaceAction = { id: 'use-as-setup', label: 'Use as setup', disabled: true };

/**
 * The analytical views of the Options setups place (#3516): the Strategy
 * Builder's fixed-leg payoff analysis, and the Strategy finder's
 * not-available page. They are not setups, so they say so in the header and
 * offer no way to save one as a setup: the one control that would is shown
 * disabled, beside the reason.
 */
@Component({
  selector: 'app-options-analysis',
  imports: [RouterOutlet, WorkspaceHeaderComponent, WorkspaceTabListComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-analysis.component.html',
  styleUrl: './options-analysis.component.scss',
})
export class OptionsAnalysisComponent {
  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly view = computed(() => optionsAnalysisViewOf(this.currentUrl()) ?? 'builder');
  protected readonly title = computed(() => OPTIONS_ANALYSIS_VIEWS.label(this.view()));
  protected readonly tabs: readonly WorkspaceTabItem[] = OPTIONS_ANALYSIS_VIEWS.tabs.map((tab) => ({ ...tab, route: optionsAnalysisRoute(tab.id) }));
  protected readonly useAsSetup = USE_AS_SETUP;
  protected readonly reason = ANALYSIS_NOT_A_SETUP_REASON;
}
