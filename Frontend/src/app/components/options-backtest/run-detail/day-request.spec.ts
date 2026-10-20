import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../../../shared/date/et-midnight';
import { dayRequestFrom, lookAtLabel, type DayRequest } from './day-request';

describe('asking to look at a day', () => {
  it('names the control that was pressed, no control for an event never delivered to one, and a new request for each press', () => {
    const button = document.createElement('button');
    const asked: DayRequest[] = [];
    // The event names its target only while it is being delivered, which is when a component handler asks.
    button.addEventListener('click', (event) => asked.push(dayRequestFrom(1234, event)));
    document.body.appendChild(button);

    button.click();

    expect.soft(asked, 'names the control that was pressed, so that closing the day can give the keyboard back to it (1 of 2)').toEqual([{ openMs: 1234, origin: button }]);
    expect.soft(asked[0].origin, 'names the control that was pressed, so that closing the day can give the keyboard back to it (2 of 2)').toBe(button);
    button.remove();

    expect.soft(dayRequestFrom(1234, new Event('click')), 'names no control for an event that was never delivered to one').toEqual({ openMs: 1234, origin: null });

    const event = new Event('click');

    expect.soft(dayRequestFrom(1234, event), 'is a new request for each press, even for the same day, so asking again is heard').not.toBe(dayRequestFrom(1234, event));
  });

  // 00:30 New York time on the 16th is still the 15th in Chicago and in Honolulu; 20:00 on the 15th is already the 16th in Tokyo.
  it.each([
    ['00:30 on the 16th', etMidnightMs('2026-09-16') + 30 * 60_000, 'Look at 2026-09-16'],
    ['20:00 on the 15th', etMidnightMs('2026-09-15') + 20 * 60 * 60_000, 'Look at 2026-09-15'],
  ])('names a control by the New York date of the day, whatever the zone of the viewer: an open at %s', (_name, openMs, label) => {
    expect(lookAtLabel(openMs)).toBe(label);
  });
});
