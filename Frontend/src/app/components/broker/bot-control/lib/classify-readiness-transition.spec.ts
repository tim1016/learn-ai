// PRD #617 — exhaustive switch coverage for classify-readiness-transition.

import { describe, expect, it } from 'vitest';

import type { ReadinessVerdictEnum } from '../../../../api/live-instances.types';

import {
  classifyReadinessTransition,
  type ReadinessTransition,
} from './classify-readiness-transition';

const VERDICTS: ReadinessVerdictEnum[] = ['READY', 'BLOCKED', 'DEGRADED', 'UNKNOWN'];

describe('classifyReadinessTransition', () => {
  it('returns initial when previous is null', () => {
    for (const v of VERDICTS) {
      expect(classifyReadinessTransition(null, v)).toBe('initial');
    }
  });

  it('returns entered-attention when READY transitions to a non-READY verdict', () => {
    for (const v of VERDICTS.filter((x) => x !== 'READY')) {
      expect(classifyReadinessTransition('READY', v)).toBe('entered-attention');
    }
  });

  it('returns recovered when non-READY transitions to READY', () => {
    for (const v of VERDICTS.filter((x) => x !== 'READY')) {
      expect(classifyReadinessTransition(v, 'READY')).toBe('recovered');
    }
  });

  it('returns attention-changed when one non-READY transitions to a different non-READY', () => {
    expect(classifyReadinessTransition('BLOCKED', 'DEGRADED')).toBe('attention-changed');
    expect(classifyReadinessTransition('DEGRADED', 'BLOCKED')).toBe('attention-changed');
    expect(classifyReadinessTransition('UNKNOWN', 'BLOCKED')).toBe('attention-changed');
  });

  it('returns stable when verdict does not change', () => {
    for (const v of VERDICTS) {
      expect(classifyReadinessTransition(v, v)).toBe('stable');
    }
  });

  it('covers every (previous, current) cell of the matrix', () => {
    const seen = new Set<ReadinessTransition>();
    for (const prev of [null, ...VERDICTS] as const) {
      for (const curr of VERDICTS) {
        seen.add(classifyReadinessTransition(prev, curr));
      }
    }
    // assertNever-style: if the predicate emits any value outside the
    // closed set, this fails on principle.
    const allowed: ReadinessTransition[] = [
      'initial',
      'entered-attention',
      'attention-changed',
      'recovered',
      'stable',
    ];
    for (const value of seen) {
      expect(allowed).toContain(value);
    }
  });
});
