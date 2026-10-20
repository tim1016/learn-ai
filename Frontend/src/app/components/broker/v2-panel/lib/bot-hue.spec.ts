import { describe, expect, it } from 'vitest';

import { BOT_HUES, botHues, type HuedSegment } from './bot-hue';

describe('botHues', () => {
  it.each<[string, HuedSegment[], (string | null)[]]>([
    ['gives each bot the hue of its registration order, wrapped onto the palette', [
      { kind: 'bot', palette_index: 7 },
      { kind: 'stopped', palette_index: 0 },
      { kind: 'bot', palette_index: 1 },
      { kind: 'free' },
    ], [BOT_HUES[1], null, BOT_HUES[1], null]],
    ['falls back to the slice’s place among this bar’s bots when no index is sent', [
      { kind: 'bot' }, { kind: 'charges' }, { kind: 'bot', palette_index: null },
    ], [
      BOT_HUES[0], null, BOT_HUES[1],
    ]],
  ])('picks each slice’s hue: %s', (_name, slices, expected) => {
    expect(botHues(slices)).toEqual(expected);
  });

  it('keeps a bot’s hue when the bots drawn before it change', () => {
    const alone = botHues([{ kind: 'bot', palette_index: 2 }]);
    const withOthers = botHues([{ kind: 'bot', palette_index: 0 }, { kind: 'bot', palette_index: 2 }]);

    expect(withOthers[1]).toBe(alone[0]);
  });
});
