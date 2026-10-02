import { describe, expect, it } from 'vitest';

import { researchStage } from './golden-search-process.component';
import { primaryAction, stepProgress } from './golden-search-steps';
import { studyDetail } from './testing/fixtures';

describe('stepProgress', () => {
  it('reads the decision step as waiting while the study needs a decision', () => {
    expect(stepProgress('decision', 'awaiting_review')).toBe('current');
    expect(stepProgress('compare', 'awaiting_review')).toBe('done');
    expect(stepProgress('decision', 'awaiting_candidate')).toBe('upcoming');
  });

  it('reads the decision step as done once a decision ended the study', () => {
    for (const state of ['approved', 'retained', 'closed'] as const) {
      expect(stepProgress('decision', state)).toBe('done');
    }
  });
});

describe('primaryAction', () => {
  it('names Run research by where the study is: waiting, a stage no worker took, or a stopped stage', () => {
    const permitted = ['run_research' as const];
    expect(primaryAction({ state: 'locked', presented_status: 'idle', permitted_actions: permitted })?.label).toBe('Run research');
    expect(primaryAction({ state: 'search_running', presented_status: 'queued', permitted_actions: permitted })?.label).toBe('Start research again');
    expect(primaryAction({ state: 'validation_running', presented_status: 'interrupted', permitted_actions: permitted })?.label).toBe('Resume research');
  });
});

describe('researchStage', () => {
  it('ends a study kept or closed early where its recorded evidence stops', () => {
    const afterSearch = studyDetail('awaiting_validation');
    expect(researchStage({ state: 'retained', results: afterSearch.results })).toBe('research');
    expect(researchStage({ state: 'retained', results: studyDetail('awaiting_candidate').results })).toBe('compare');
    expect(researchStage({ state: 'closed', results: studyDetail('awaiting_review').results })).toBe('decision');
    expect(researchStage({ state: 'approved', results: studyDetail('approved').results })).toBe('deploy');
  });
});
