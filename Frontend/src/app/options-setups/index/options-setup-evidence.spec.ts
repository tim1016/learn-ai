import { describe, expect, it } from 'vitest';

import { approvalOf, indexOf, runOf, savedName, studyOf } from '../testing/fixtures';
import { evidenceHolding, holdersOf, namesHolding, newestFirst } from './options-setup-evidence';

describe('joining evidence to saved setups by exact content', () => {
  const index = indexOf({
    setups: [savedName('Alpha', '1'), savedName('Beta', '1'), savedName('Gamma', '2'), savedName('Broken', null)],
    runs: [runOf(5, '1', { recorded_name: 'Alpha' }), runOf(6, '2'), runOf(7, '3'), runOf(8, null)],
    studies: [studyOf('1')],
    approvals: [approvalOf('1')],
  });

  it('gives identical content under two names the same names and the same evidence, and picks no owner', () => {
    expect(namesHolding(index, '1'.repeat(64)).map((setup) => setup.name)).toEqual(['Alpha', 'Beta']);
    const held = evidenceHolding(index, '1'.repeat(64));

    expect(held.runs.map((run) => run.id)).toEqual([5]);
    expect(held.studies).toHaveLength(1);
    expect(held.approvals).toHaveLength(1);
  });

  it('does not link a run to the name it was recorded under when its content is different', () => {
    const edited = indexOf({ setups: [savedName('Alpha', '9')], runs: [runOf(5, '1', { recorded_name: 'Alpha' })] });

    expect(evidenceHolding(edited, '9'.repeat(64)).runs).toEqual([]);
    expect(holdersOf(edited, edited.runs[0])).toEqual({ kind: 'none' });
  });

  it('says no saved name holds content that was edited away or deleted, and says so apart from content that cannot be read', () => {
    const [orphan, unreadable] = [index.runs[2], index.runs[3]];

    expect(holdersOf(index, orphan)).toEqual({ kind: 'none' });
    expect(holdersOf(index, unreadable)).toEqual({ kind: 'unreadable', reason: 'legs: Field required' });
    expect(holdersOf(index, index.runs[0])).toMatchObject({ kind: 'names' });
  });

  it('finds nothing for a setup that cannot be opened, because it has no content to match', () => {
    expect(namesHolding(index, null)).toEqual([]);
    expect(evidenceHolding(index, undefined)).toEqual({ runs: [], studies: [], approvals: [] });
  });

  it('lists newest first, and breaks a tie by identity', () => {
    const rows = [runOf(1, '1', { created_at_ms: 10 }), runOf(2, '1', { created_at_ms: 20 }), runOf(3, '1', { created_at_ms: 20 })];

    expect(newestFirst(rows).map((run) => run.id)).toEqual([3, 2, 1]);
  });
});
