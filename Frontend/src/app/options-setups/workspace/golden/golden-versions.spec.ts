import { describe, expect, it } from 'vitest';

import { approvalOf, indexOf, runOf, savedName, STUDY_ID, studyOf } from '../../testing/fixtures';
import { reviewableRuns, reviewedRunId, verdictWords, versionsOfSetup } from './golden-versions';

describe('versionsOfSetup', () => {
  it('lists a version by the content it froze, and a changed candidate through the study that began from this content', () => {
    const index = indexOf({
      setups: [savedName('Mine', '1')],
      studies: [studyOf('1'), studyOf('2', { id: 'os_other' })],
      approvals: [
        approvalOf('1', { id: 'exact-old', created_at_ms: 10 }),
        approvalOf('5', { id: 'changed', study_id: STUDY_ID, origin: 'search', created_at_ms: 20 }),
        approvalOf('1', { id: 'exact-new', created_at_ms: 30, study_id: STUDY_ID, origin: 'search' }),
        approvalOf('6', { id: 'from-another-seed', study_id: 'os_other', origin: 'search' }),
        approvalOf('7', { id: 'other', recorded_name: 'Mine' }),
      ],
    });

    const found = versionsOfSetup(index, '1'.repeat(64));

    expect(found.exact.map((listed) => listed.approval.id)).toEqual(['exact-new', 'exact-old']);
    expect(found.throughStudy.map((listed) => listed.approval.id)).toEqual(['changed']);
    expect(found.throughStudy[0]?.relation).toMatchObject({ kind: 'study', study: { id: STUDY_ID } });
    expect(found.studies.map((study) => study.id)).toEqual([STUDY_ID]);
  });

  it('lists nothing for a content that is not known', () => {
    const index = indexOf({ approvals: [approvalOf('1')], studies: [studyOf('1')] });

    expect(versionsOfSetup(index, null)).toEqual({ exact: [], throughStudy: [], studies: [] });
    expect(versionsOfSetup(index, undefined).exact).toEqual([]);
  });
});

describe('reviewableRuns', () => {
  it('is the completed runs that froze exactly this content, newest first', () => {
    const index = indexOf({ runs: [runOf(7, '1'), runOf(8, '2'), runOf(9, '1'), runOf(10, '1', { status: 'failed' }), runOf(11, null)] });

    expect(reviewableRuns(index, '1'.repeat(64)).map((run) => run.id)).toEqual([9, 7]);
    expect(reviewableRuns(index, null)).toEqual([]);
  });
});

describe('reviewedRunId', () => {
  it.each([
    ['7', 7],
    ['76', 76],
    ['123456789012', 123456789012],
  ])('reads %s as run %d', (raw, id) => {
    expect(reviewedRunId(raw)).toBe(id);
  });

  it.each([undefined, null, '', '0', '-3', '7.5', '07', '7 ', 'abc', '1e3', '9'.repeat(16)])('reads %j as no run', (raw) => {
    expect(reviewedRunId(raw)).toBeNull();
  });
});

describe('verdictWords', () => {
  it('says only what the study recorded, and nothing when it recorded none', () => {
    expect(verdictWords({ verdict: 'meets_rules' } as never)).toBe('The search recorded that this candidate met its rules.');
    expect(verdictWords({ verdict: 'owner_exception' } as never)).toContain('did not meet every rule');
    expect(verdictWords({ verdict: null } as never)).toBeNull();
  });
});
