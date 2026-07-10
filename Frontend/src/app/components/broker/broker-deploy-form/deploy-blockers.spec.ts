import { describe, expect, it, vi } from 'vitest';
import type { OperatorBlocker } from '../../../api/operator-blocker.types';
import { operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';
import { buildFormBlockers, deployReady, resolveBlockerMove } from './deploy-blockers';

describe('buildFormBlockers', () => {
  const ready = {
    missingRequiredFields: [] as string[],
    identityConflictSummary: null,
    exposureConflictSummary: null,
    customSizingError: null,
    actionPlanReady: true,
  };

  it('returns no blockers when the form is complete', () => {
    expect(buildFormBlockers(ready)).toEqual([]);
  });

  it('emits a blocking fix_here blocker listing missing fields', () => {
    const blockers = buildFormBlockers({
      ...ready,
      missingRequiredFields: ['Strategy', 'Deployment name'],
    });

    expect(blockers).toHaveLength(1);
    expect(blockers[0].condition.id).toBe('missing_required_fields');
    expect(blockers[0].condition.severity).toBe('blocking');
    expect(blockers[0].disposition).toBe('fix_here');
    expect(blockers[0].detail).toContain('Strategy');
  });

  it('emits an identity-coherence blocker with a confirm-in-form move', () => {
    const blockers = buildFormBlockers({
      ...ready,
      identityConflictSummary: 'Symbol mismatch',
    });
    const match = blockers.find((b) => b.condition.id === 'identity_coherence_unconfirmed');

    expect(match?.disposition).toBe('fix_here');
    expect(match?.primary_move?.action.kind).toBe('confirm_in_form');
  });
});

describe('deployReady', () => {
  it('is false when any blocker is blocking', () => {
    const b: OperatorBlocker = operatorBlockerFixture({
      id: 'x',
      host: 'deploy_preflight',
      primaryMove: null,
      appliesTo: 'deploy',
    });

    expect(deployReady([b])).toBe(false);
  });

  it('is true when all blockers are warnings', () => {
    const b: OperatorBlocker = operatorBlockerFixture({
      id: 'x',
      host: 'deploy_preflight',
      severity: 'warning',
      disposition: 'wait',
      primaryMove: null,
      appliesTo: 'deploy',
    });

    expect(deployReady([b])).toBe(true);
  });
});

describe('resolveBlockerMove', () => {
  it('navigates for a navigate action', () => {
    const navigate = vi.fn();
    const rendered = resolveBlockerMove(
      {
        label: 'Connect the broker',
        action: { kind: 'navigate', route: '/broker', fragment: null },
        target: null,
      },
      { navigate, focusAnchor: vi.fn() },
    );

    rendered?.invoke();

    expect(navigate).toHaveBeenCalledWith('/broker', null);
    expect(rendered?.variant).toBe('link');
  });

  it('focuses an anchor for a confirm_in_form action', () => {
    const focusAnchor = vi.fn();
    const rendered = resolveBlockerMove(
      {
        label: 'Confirm identity',
        action: { kind: 'confirm_in_form', anchor: 'coherence-card' },
        target: null,
      },
      { navigate: vi.fn(), focusAnchor },
    );

    rendered?.invoke();

    expect(focusAnchor).toHaveBeenCalledWith('coherence-card');
    expect(rendered?.variant).toBe('primary');
  });
});
