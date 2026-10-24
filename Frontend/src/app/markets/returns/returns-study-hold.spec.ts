import { describe, expect, it } from 'vitest';

import type { ReturnDistributionStudy, StudyQuery } from './returns-distribution.service';
import { ReturnsStudyHold } from './returns-study-hold';

function study(capture: ReturnDistributionStudy['capture']): ReturnDistributionStudy {
  return {
    adjustment: 'split_and_dividend',
    warnings: [],
    capture,
    coverage: {
      requestedSessions: 0,
      returnedSessions: 0,
      missingSessions: 0,
      excludedSessions: 0,
      firstSessionOpenMsUtc: null,
      lastSessionOpenMsUtc: null,
    },
    kinds: [],
    days: [],
  };
}

const query = (overrides: Partial<StudyQuery> = {}): StudyQuery => ({
  symbol: 'SPY',
  fromMsUtc: 1,
  toMsUtc: 2,
  binWidthPct: 0.5,
  spanPct: 5,
  ...overrides,
});

describe('ReturnsStudyHold', () => {
  it('hands back the study of exactly the request it was given, and none for a different one', () => {
    const hold = new ReturnsStudyHold();
    const held = study({ status: 'complete', fetchedArtifactCount: 3, detail: null });
    hold.put(query(), held);

    expect(hold.get(query())).toBe(held);
    expect(hold.get(query({ binWidthPct: 1 }))).toBeUndefined();
    expect(hold.get(query({ toMsUtc: 3 }))).toBeUndefined();
    expect(hold.get(query({ symbol: 'QQQ' }))).toBeUndefined();
  });

  it.each(['partial', 'failed'] as const)('never holds a study whose lake capture ended %s, so reopening asks again', (status) => {
    const hold = new ReturnsStudyHold();
    hold.put(query(), study({ status, fetchedArtifactCount: 0, detail: 'timeout' }));

    expect(hold.get(query())).toBeUndefined();
  });

  it.each(['not_attempted', 'skipped'] as const)('holds a study whose capture was %s: nothing was owed', (status) => {
    const hold = new ReturnsStudyHold();
    hold.put(query(), study({ status, fetchedArtifactCount: 0, detail: null }));

    expect(hold.get(query())).toBeDefined();
  });

  it('forgets one instrument\'s studies when its lake changed, and keeps the others', () => {
    const hold = new ReturnsStudyHold();
    const settled = study(null);
    hold.put(query(), settled);
    hold.put(query({ symbol: 'QQQ' }), settled);

    hold.invalidate('SPY');

    expect(hold.get(query())).toBeUndefined();
    expect(hold.get(query({ symbol: 'QQQ' }))).toBe(settled);
  });

  it('keeps a bounded number, pushing out the one used longest ago', () => {
    const hold = new ReturnsStudyHold();
    const settled = study(null);
    for (let i = 1; i <= 6; i += 1) hold.put(query({ toMsUtc: i }), settled);
    hold.get(query({ toMsUtc: 1 })); // used again: no longer the oldest
    hold.put(query({ toMsUtc: 7 }), settled);

    expect(hold.get(query({ toMsUtc: 1 }))).toBe(settled);
    expect(hold.get(query({ toMsUtc: 2 }))).toBeUndefined();
    expect(hold.get(query({ toMsUtc: 7 }))).toBe(settled);
  });
});
