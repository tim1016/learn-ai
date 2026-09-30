import { describe, expect, it } from 'vitest';

import { botEndFields, botEndInput } from './bot-end-fields';

const AT = new Date(2026, 8, 29, 14, 59).getTime();

describe('a bot’s end as the owner edits it (#2607)', () => {
  it('reads an end into the viewer’s own date and time, and back to the same instant', () => {
    const fields = botEndFields({ end_at_ms: AT, end_action: 'KEEP' });

    expect(fields).toEqual({ date: '2026-09-29', clock: '14:59', noEnd: false, action: 'KEEP' });
    expect(botEndInput(fields)).toEqual({ end_at_ms: AT, end_action: 'KEEP' });
  });

  it('keeps the last date and time under "no end", so unticking it brings them back', () => {
    const before = botEndFields({ end_at_ms: AT, end_action: 'SELL' });

    const noEnd = botEndFields({ end_at_ms: null, end_action: 'SELL' }, before);

    expect(noEnd).toEqual({ ...before, noEnd: true });
    expect(botEndInput(noEnd)).toEqual({ end_at_ms: null, end_action: 'SELL' });
    expect(botEndInput({ ...noEnd, noEnd: false })).toEqual({ end_at_ms: AT, end_action: 'SELL' });
  });

  it('always sends the action, and Sell where Keep is not offered', () => {
    const keep = botEndFields({ end_at_ms: AT, end_action: 'KEEP' });

    expect(botEndInput(keep, false)).toEqual({ end_at_ms: AT, end_action: 'SELL' });
    expect(botEndInput({ ...keep, noEnd: true }, false)).toEqual({ end_at_ms: null, end_action: 'SELL' });
  });

  it('sends "no end" with Sell even after Keep was chosen, since no end has no shares to keep', () => {
    const keep = botEndFields({ end_at_ms: AT, end_action: 'KEEP' });

    expect(botEndInput({ ...keep, noEnd: true })).toEqual({ end_at_ms: null, end_action: 'SELL' });
  });

  it('sends nothing while the date or time is not a real wall clock', () => {
    const fields = botEndFields({ end_at_ms: AT, end_action: 'SELL' });

    expect(botEndInput({ ...fields, date: '' })).toBeNull();
    expect(botEndInput({ ...fields, clock: '25:00' })).toBeNull();
  });
});
