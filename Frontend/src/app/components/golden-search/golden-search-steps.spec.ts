import { describe, expect, it } from 'vitest';

import { stepProgress } from './golden-search-steps';

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
