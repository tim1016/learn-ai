import { describeUniverseHolding } from './universe-holding';

describe('describeUniverseHolding', () => {
  it('says nothing when the lake holds every session of every member', () => {
    expect(describeUniverseHolding({ members: 5, lacking: 0, memberSessions: 0 })).toBeNull();
  });

  it('adds the member-sessions up, names how many members lack some, and says why the first run is slow', () => {
    const content = describeUniverseHolding({ members: 5, lacking: 2, memberSessions: 42 });

    expect(content?.kind).toBe('partial');
    expect(content?.happened).toBe('42 member-sessions of stock bars aren’t in the lake yet, across 2 of 5 members.');
    expect(content?.why).toContain('about one vendor call per member-session');
  });

  it('speaks in the singular for one member-session of one member', () => {
    expect(describeUniverseHolding({ members: 1, lacking: 1, memberSessions: 1 })?.happened).toBe(
      '1 member-session of stock bars isn’t in the lake yet, across 1 of 1 member.',
    );
  });
});
