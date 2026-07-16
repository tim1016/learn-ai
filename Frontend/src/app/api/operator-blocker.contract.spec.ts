import { describe, expect, it } from 'vitest';

import {
  OPERATOR_BLOCKER_ANCHOR_KINDS,
  accountDeskAnchorOrVerdictFallback,
  operatorAttentionConditionCount,
  operatorBlockersForAccountDeskLens,
  type OperatorBlocker,
} from './operator-blocker.types';

const CONTRACT_BLOCKER: OperatorBlocker = {
  condition: {
    id: 'fleet_contaminated',
    severity: 'blocking',
    scope: 'fleet',
    evidence: {},
  },
  host: 'account_desk',
  anchor: { kind: 'reconciliation', subject_key: null },
  audience: 'operator',
  disposition: 'fix_elsewhere',
  headline: 'Fleet state blocks starts',
  detail: 'Clear the account fleet state before starting another bot.',
  primary_move: {
    label: 'Open Accounts',
    action: { kind: 'navigate', route: '/broker/accounts', fragment: null },
    target: null,
  },
  secondary_moves: [],
  applies_to: 'both',
};

describe('OperatorBlocker contract mirror', () => {
  it('pins every required blocker field and the closed anchor vocabulary', () => {
    expect(Object.keys(CONTRACT_BLOCKER).sort()).toEqual([
      'anchor',
      'applies_to',
      'audience',
      'condition',
      'detail',
      'disposition',
      'headline',
      'host',
      'primary_move',
      'secondary_moves',
    ]);
    expect(OPERATOR_BLOCKER_ANCHOR_KINDS).toEqual([
      'surface',
      'verdict',
      'lease',
      'clerk',
      'reconciliation',
      'holdings_row',
      'event',
      'cure_tools',
    ]);
  });

  it('preserves valid opaque subject keys and falls back from future anchor kinds', () => {
    expect(
      accountDeskAnchorOrVerdictFallback({
        kind: 'holdings_row',
        subject_key: 'DU123|con_id:265598|SPY  260620C00500000',
      }),
    ).toEqual({
      kind: 'holdings_row',
      subject_key: 'DU123|con_id:265598|SPY  260620C00500000',
    });
    expect(
      accountDeskAnchorOrVerdictFallback({ kind: 'new_future_anchor', subject_key: 'opaque:77' }),
    ).toEqual({ kind: 'verdict', subject_key: null });
    expect(accountDeskAnchorOrVerdictFallback({ kind: 'verdict', subject_key: 'DU123' })).toBeNull();
  });

  it('routes full guidance by audience and deduplicates operator attention by condition identity', () => {
    const traderProjection: OperatorBlocker = {
      ...CONTRACT_BLOCKER,
      audience: 'trader',
      headline: 'Trader-facing fleet warning',
    };
    const operatorProjection: OperatorBlocker = {
      ...CONTRACT_BLOCKER,
      audience: 'operator',
      headline: 'Operator fleet cure',
    };
    const bothProjection: OperatorBlocker = {
      ...CONTRACT_BLOCKER,
      condition: { ...CONTRACT_BLOCKER.condition, id: 'account_frozen' },
      audience: 'both',
    };

    expect(operatorBlockersForAccountDeskLens(
      [traderProjection, operatorProjection, bothProjection],
      'trader',
    )).toEqual([traderProjection, bothProjection]);
    expect(operatorBlockersForAccountDeskLens(
      [traderProjection, operatorProjection, bothProjection],
      'operator',
    )).toEqual([operatorProjection, bothProjection]);
    expect(operatorAttentionConditionCount([
      operatorProjection,
      { ...operatorProjection, anchor: { kind: 'verdict', subject_key: null } },
      bothProjection,
    ])).toBe(1);
  });
});
