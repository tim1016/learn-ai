import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { StudyDetail, StudyState } from './golden-search.types';

/** The research process a Golden Search study follows (#2811), from the plan to a deployable configuration. */
export const RESEARCH_PROCESS = [
  { id: 'plan', label: 'Plan' },
  { id: 'research', label: 'Research' },
  { id: 'compare', label: 'Compare' },
  { id: 'final', label: 'Final test' },
  { id: 'decision', label: 'Decision' },
  { id: 'deploy', label: 'Deploy' },
] as const;

export type ResearchStage = (typeof RESEARCH_PROCESS)[number]['id'];

const STAGE_OF_STATE: Readonly<Record<StudyState, ResearchStage>> = {
  locked: 'research',
  search_running: 'research',
  awaiting_validation: 'research',
  validation_running: 'research',
  awaiting_candidate: 'compare',
  candidate_locked: 'final',
  exam_running: 'final',
  awaiting_review: 'decision',
  qualification_pending: 'decision',
  qualification_failed: 'decision',
  retained: 'decision',
  closed: 'decision',
  approved: 'deploy',
};

/**
 * Where a study sits in the research process; an approved study's next place
 * is Deploy. A study kept or closed early ends where its recorded evidence
 * stops, so stages it never ran are not shown as done (#2814 review).
 */
export function researchStage(study: Pick<StudyDetail, 'state' | 'results'>): ResearchStage {
  if (study.state !== 'retained' && study.state !== 'closed') return STAGE_OF_STATE[study.state];
  if (study.results.exam !== null) return 'decision';
  return study.results.evidence !== null ? 'compare' : 'research';
}

/** Where the user is in the research process; finished stages are marked by text and icon, not colour alone. */
@Component({
  selector: 'app-golden-search-process',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-process.component.html',
  styleUrl: './golden-search-process.component.scss',
})
export class GoldenSearchProcessComponent {
  readonly current = input.required<ResearchStage>();

  protected readonly stages = computed(() => {
    const at = RESEARCH_PROCESS.findIndex((stage) => stage.id === this.current());
    return RESEARCH_PROCESS.map((stage, index) => ({ ...stage, state: index < at ? 'done' : index === at ? 'current' : 'upcoming' }));
  });
}
