import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { StudyGuidance } from './golden-search.types';

/**
 * PLACEHOLDER (#2696, workbench part 2 replaces it): the Final decision step
 * renders only the server's guidance. Opening the final test, the exam
 * results, approval and the Deploy handoff are not built here.
 */
@Component({
  selector: 'app-golden-search-decision-step',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<p class="guidance"><strong>{{ guidance().headline }}</strong> {{ guidance().detail }}</p>`,
  styles: `.guidance { margin: 0; font-size: var(--fs-sm); }`,
})
export class GoldenSearchDecisionStepComponent {
  readonly guidance = input.required<StudyGuidance>();
}
