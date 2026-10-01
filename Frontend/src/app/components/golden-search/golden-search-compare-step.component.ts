import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { StudyGuidance } from './golden-search.types';

/**
 * PLACEHOLDER (#2696, workbench part 2 replaces it): the Compare step renders
 * only the server's guidance. The candidate table, recommendation and evidence
 * tabs are not built here.
 */
@Component({
  selector: 'app-golden-search-compare-step',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<p class="guidance"><strong>{{ guidance().headline }}</strong> {{ guidance().detail }}</p>`,
  styles: `.guidance { margin: 0; font-size: var(--fs-sm); }`,
})
export class GoldenSearchCompareStepComponent {
  readonly guidance = input.required<StudyGuidance>();
}
