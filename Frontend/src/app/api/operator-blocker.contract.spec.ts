import { describe, expect, it } from 'vitest';

import {
  OPERATOR_BLOCKER_ANCHOR_KINDS,
  movesForBlocker,
  type OperatorBlocker,
  type OperatorMove,
} from './operator-blocker.types';

const CONTRACT_BLOCKER: OperatorBlocker = {
  condition: {
    id: 'fleet_contaminated',
    severity: 'blocking',
    scope: 'fleet',
    evidence: {},
  },
  host: 'bot_cockpit',
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

  it('includes secondary moves for every non-wait disposition, not just terminal', () => {
    const secondaryMove: OperatorMove = {
      label: 'Open runbook',
      action: { kind: 'open_runbook', slug: 'fleet-contamination' },
      target: null,
    };
    const fixHere: OperatorBlocker = {
      ...CONTRACT_BLOCKER,
      disposition: 'fix_here',
      secondary_moves: [secondaryMove],
    };
    const fixElsewhere: OperatorBlocker = {
      ...CONTRACT_BLOCKER,
      disposition: 'fix_elsewhere',
      secondary_moves: [secondaryMove],
    };
    const waiting: OperatorBlocker = {
      ...CONTRACT_BLOCKER,
      disposition: 'wait',
      primary_move: null,
      secondary_moves: [secondaryMove],
    };

    expect(movesForBlocker(fixHere)).toEqual([CONTRACT_BLOCKER.primary_move, secondaryMove]);
    expect(movesForBlocker(fixElsewhere)).toEqual([CONTRACT_BLOCKER.primary_move, secondaryMove]);
    expect(movesForBlocker(waiting)).toEqual([]);
  });
});
