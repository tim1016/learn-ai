import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';

import { botEndFields, botEndInput, botEndRefusal } from './bot-end-fields';

const AT = new Date(2026, 8, 29, 14, 59).getTime();

describe('a bot’s end as the owner edits it (#2607)', () => {
  it('reads an end into the viewer’s own date and time, and back to the same instant', () => {
    const fields = botEndFields({ end_at_ms: AT, end_action: 'KEEP' });

    expect(fields).toEqual({ date: '2026-09-29', time: '14:59', noEnd: false, action: 'KEEP' });
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

  it('sends nothing while the date or time is not a real wall clock', () => {
    const fields = botEndFields({ end_at_ms: AT, end_action: 'SELL' });

    expect(botEndInput({ ...fields, date: '' })).toBeNull();
    expect(botEndInput({ ...fields, time: '25:00' })).toBeNull();
  });

  it('reads a refusal in the panel’s words and the fleet’s, never inventing any', () => {
    const panel = botEndRefusal(new HttpErrorResponse({
      status: 409,
      error: { detail: { message: 'This bot has already ended.', why: 'Its end has come.', next_action: null, reason_code: 'BOT_END_REFUSED' } },
    }), 'fallback');
    const fleet = botEndRefusal(new HttpErrorResponse({
      status: 409,
      error: { reason: 'clerk_binding_generation_conflict', message: 'The account changed.', next_step: 'Refresh the page.' },
    }), 'fallback');

    expect(panel).toEqual({ message: 'This bot has already ended.', why: 'Its end has come.', nextAction: null, reasonCode: 'BOT_END_REFUSED' });
    expect(fleet).toEqual({ message: 'The account changed.', why: null, nextAction: 'Refresh the page.', reasonCode: 'clerk_binding_generation_conflict' });
    expect(botEndRefusal(new HttpErrorResponse({ status: 0 }), 'fallback').message).toBe('fallback');
  });
});
