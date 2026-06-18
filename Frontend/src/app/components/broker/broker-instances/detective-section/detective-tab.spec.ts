import { describe, expect, it } from 'vitest';

import { deriveActiveTab } from './detective-tab';

describe('deriveActiveTab', () => {
  it('defaults to "activity" when no query param is present', () => {
    expect(deriveActiveTab(null)).toBe('activity');
  });

  it('returns "activity" when the query param is "activity"', () => {
    expect(deriveActiveTab('activity')).toBe('activity');
  });

  it('returns "diagnostics" when the query param is "diagnostics"', () => {
    expect(deriveActiveTab('diagnostics')).toBe('diagnostics');
  });

  it('falls back to "activity" for unrecognized values', () => {
    expect(deriveActiveTab('something-else')).toBe('activity');
  });

  it('falls back to "activity" for an empty string', () => {
    expect(deriveActiveTab('')).toBe('activity');
  });
});
