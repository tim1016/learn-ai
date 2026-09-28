import { describe, expect, it } from 'vitest';

import { BOT_HUES, botHues } from './bot-hue';

describe('botHues', () => {
  it('gives each bot the hue of its registration order, wrapped onto the palette', () => {
    expect(botHues([
      { kind: 'bot', palette_index: 7 },
      { kind: 'stopped', palette_index: 0 },
      { kind: 'bot', palette_index: 1 },
      { kind: 'free' },
    ])).toEqual([BOT_HUES[1], null, BOT_HUES[1], null]);
  });

  it('keeps a bot’s hue when the bots drawn before it change', () => {
    const alone = botHues([{ kind: 'bot', palette_index: 2 }]);
    const withOthers = botHues([{ kind: 'bot', palette_index: 0 }, { kind: 'bot', palette_index: 2 }]);

    expect(withOthers[1]).toBe(alone[0]);
  });

  it('falls back to the slice’s place among this bar’s bots when no index is sent', () => {
    expect(botHues([{ kind: 'bot' }, { kind: 'charges' }, { kind: 'bot', palette_index: null }])).toEqual([
      BOT_HUES[0], null, BOT_HUES[1],
    ]);
  });
});
