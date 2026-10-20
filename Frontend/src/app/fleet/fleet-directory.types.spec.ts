import { describe, expect, it } from 'vitest';

import { testLane } from './fleet-directory-testing';
import { laneAttention, laneDisplayName, laneDisplayNameText, type LaneDescriptor } from './fleet-directory.types';

function lane(overrides: Partial<LaneDescriptor> = {}): LaneDescriptor {
  return testLane(overrides);
}

describe('laneDisplayName', () => {
  it.each([
    ['uses the nickname when the lane has one', {
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    }, { name: 'Strategy lab', disambiguator: null }],
    ['falls back to the lane label when no nickname is set', {
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: null },
    }, { name: 'Paper', disambiguator: null }],
    ['falls back to the lane label for a lane with no confirmed account at all', {
      clerk_id: 'clrk_unbound',
      display_label: 'Unbound lane',
      lifecycle_state: 'starting',
      provider_summary: null,
    }, { name: 'Unbound lane', disambiguator: null }],
    ['does not disambiguate against itself', {
      clerk_id: 'clrk_solo',
      display_label: 'Solo',
      provider_summary: { account_nickname: 'Only account' },
    }, { name: 'Only account', disambiguator: null }],
  ] as const)('names a lane listed alone: %s', (_name, overrides, expected) => {
    const subject = lane(overrides);

    expect(laneDisplayName(subject, [subject])).toEqual(expected);
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
  it.each([
    ['is just the name when there is no disambiguator', { name: 'Strategy lab', disambiguator: null }, 'Strategy lab'],
    ['appends the disambiguator in the same "(Label)" parenthetical the visible markup uses',
      { name: 'Strategy lab', disambiguator: 'Paper' }, 'Strategy lab (Paper)'],
  ])('renders the display name as text: %s', (_name, displayName, expected) => {
    expect(laneDisplayNameText(displayName)).toBe(expected);
  });
});

describe('laneAttention', () => {
  it.each([
    ['a count above zero', 3, { kind: 'count', count: 3 }],
    ['zero', 0, { kind: 'none' }],
    ['a null count', null, { kind: 'unknown' }],
    ['an absent count', undefined, { kind: 'unknown' }],
  ] as const)('reads %s as the pill and the card both show it', (_name, count, expected) => {
    const summary = count === undefined ? {} : { attention_count: count };

    expect(laneAttention(lane({ provider_summary: summary }))).toEqual(expected);
  });

  it('reads a lane with no provider summary as unknown, never as zero', () => {
    expect(laneAttention(lane({ provider_summary: undefined }))).toEqual({ kind: 'unknown' });
  });
});
