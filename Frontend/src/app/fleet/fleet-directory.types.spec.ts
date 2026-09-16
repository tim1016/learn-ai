import { describe, expect, it } from 'vitest';

import { testLane } from './fleet-directory-testing';
import { laneDisplayName, laneDisplayNameText, type LaneDescriptor } from './fleet-directory.types';

function lane(overrides: Partial<LaneDescriptor> = {}): LaneDescriptor {
  return testLane(overrides);
}

describe('laneDisplayName', () => {
  it('uses the nickname when the lane has one', () => {
    const paper = lane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });

    expect(laneDisplayName(paper, [paper])).toEqual({ name: 'Strategy lab', disambiguator: null });
  });

  it('falls back to the lane label when no nickname is set', () => {
    const paper = lane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: null },
    });

    expect(laneDisplayName(paper, [paper])).toEqual({ name: 'Paper', disambiguator: null });
  });

  it('falls back to the lane label for a lane with no confirmed account at all', () => {
    const unbound = lane({
      clerk_id: 'clrk_unbound',
      display_label: 'Unbound lane',
      lifecycle_state: 'starting',
      provider_summary: null,
    });

    expect(laneDisplayName(unbound, [unbound])).toEqual({ name: 'Unbound lane', disambiguator: null });
  });

  it('shows the lane label beside a name shared with another lane, case- and whitespace-insensitively', () => {
    const paper = lane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy Lab' },
    });
    const live = lane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    const all = [paper, live];

    expect(laneDisplayName(paper, all)).toEqual({ name: 'Strategy Lab', disambiguator: 'Paper' });
    expect(laneDisplayName(live, all)).toEqual({ name: 'strategy lab', disambiguator: 'Live' });
  });

  it('does not disambiguate against itself', () => {
    const solo = lane({
      clerk_id: 'clrk_solo',
      display_label: 'Solo',
      provider_summary: { account_nickname: 'Only account' },
    });

    expect(laneDisplayName(solo, [solo])).toEqual({ name: 'Only account', disambiguator: null });
  });

  it('does not disambiguate two distinct names', () => {
    const paper = lane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = lane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: 'Income desk' },
    });

    expect(laneDisplayName(paper, [paper, live]).disambiguator).toBeNull();
    expect(laneDisplayName(live, [paper, live]).disambiguator).toBeNull();
  });

  it('suppresses a disambiguator that would just repeat the name (no nickname, colliding raw labels)', () => {
    const first = lane({
      clerk_id: 'clrk_first',
      display_label: 'Paper',
      provider_summary: { account_nickname: null },
    });
    const second = lane({
      clerk_id: 'clrk_second',
      display_label: 'Paper',
      provider_summary: { account_nickname: null },
    });

    expect(laneDisplayName(first, [first, second])).toEqual({ name: 'Paper', disambiguator: null });
    expect(laneDisplayName(second, [first, second])).toEqual({ name: 'Paper', disambiguator: null });
  });

  it('suppresses the disambiguator case- and whitespace-insensitively too, matching the collision check', () => {
    // Thermo review follow-up: the suppression guard used to compare `name`
    // to `lane.display_label` with exact `!==`, while the collision check
    // above it was already case- and whitespace-insensitive — a label
    // differing only by case or padding would collide but not suppress,
    // rendering the still-uninformative "paper (Paper)".
    const first = lane({
      clerk_id: 'clrk_first',
      display_label: 'Paper',
      provider_summary: { account_nickname: null },
    });
    const second = lane({
      clerk_id: 'clrk_second',
      display_label: '  paper  ',
      provider_summary: { account_nickname: null },
    });

    expect(laneDisplayName(first, [first, second]).disambiguator).toBeNull();
    expect(laneDisplayName(second, [first, second]).disambiguator).toBeNull();
  });
});

describe('laneDisplayNameText', () => {
  it('is just the name when there is no disambiguator', () => {
    expect(laneDisplayNameText({ name: 'Strategy lab', disambiguator: null })).toBe('Strategy lab');
  });

  it('appends the disambiguator in the same "(Label)" parenthetical the visible markup uses', () => {
    expect(laneDisplayNameText({ name: 'Strategy lab', disambiguator: 'Paper' })).toBe(
      'Strategy lab (Paper)',
    );
  });
});
