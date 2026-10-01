/**
 * The workbench's five steps and the one next action each study state offers
 * (#2696). Which commands are allowed is the server's answer
 * (`permitted_actions`); this module only maps a state to the step that
 * holds its decision and names the action in operator copy.
 */

import type { StudyCommandName, StudyDetail, StudyState } from './golden-search.types';

export type StudyStep = 'plan' | 'search' | 'test' | 'compare' | 'decision';

export interface StudyStepDefinition {
  readonly id: StudyStep;
  readonly label: string;
}

export const STUDY_STEPS: readonly StudyStepDefinition[] = [
  { id: 'plan', label: 'Plan' },
  { id: 'search', label: 'Search' },
  { id: 'test', label: 'Test over time' },
  { id: 'compare', label: 'Compare' },
  { id: 'decision', label: 'Final decision' },
];

const STEP_OF_STATE: Readonly<Record<StudyState, StudyStep>> = {
  locked: 'search',
  search_running: 'search',
  awaiting_validation: 'search',
  validation_running: 'test',
  awaiting_candidate: 'compare',
  candidate_locked: 'decision',
  exam_running: 'decision',
  awaiting_review: 'decision',
  qualification_pending: 'decision',
  approved: 'decision',
  qualification_failed: 'decision',
  retained: 'decision',
  closed: 'decision',
};

/** The step that holds the decision a study in `state` is waiting on. */
export function stepForState(state: StudyState): StudyStep {
  return STEP_OF_STATE[state];
}

export type StepProgress = 'done' | 'current' | 'upcoming';

export function stepProgress(step: StudyStep, state: StudyState): StepProgress {
  const order = STUDY_STEPS.map((s) => s.id);
  const current = order.indexOf(stepForState(state));
  const index = order.indexOf(step);
  if (index < current) return 'done';
  return index === current ? 'current' : 'upcoming';
}

export type PrimaryAction =
  | { readonly kind: 'command'; readonly command: Extract<StudyCommandName, 'continue'>; readonly label: string }
  | { readonly kind: 'step'; readonly step: StudyStep; readonly label: string };

const CONTINUE_LABELS: Partial<Readonly<Record<StudyState, string>>> = {
  locked: 'Start the search',
  awaiting_validation: 'Test over time',
};

const STEP_ACTIONS: Partial<Readonly<Record<StudyState, PrimaryAction>>> = {
  awaiting_candidate: { kind: 'step', step: 'compare', label: 'Compare candidates' },
  candidate_locked: { kind: 'step', step: 'decision', label: 'Review the final test' },
  awaiting_review: { kind: 'step', step: 'decision', label: 'Make the final decision' },
  qualification_failed: { kind: 'step', step: 'decision', label: 'Make the final decision' },
  approved: { kind: 'step', step: 'decision', label: 'See the golden configuration' },
};

/**
 * The single forward action for this study, or null when there is none to
 * take (a stage is running, or the study is finished). Cancel, Finish and
 * Hide are record controls, not the next step.
 */
export function primaryAction(study: Pick<StudyDetail, 'state' | 'permitted_actions'>): PrimaryAction | null {
  const label = CONTINUE_LABELS[study.state];
  if (label !== undefined) return study.permitted_actions.includes('continue') ? { kind: 'command', command: 'continue', label } : null;
  return STEP_ACTIONS[study.state] ?? null;
}
