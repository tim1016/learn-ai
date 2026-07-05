import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { TimestampDisplayComponent } from './timestamp-display.component';
import { formatTimestampDisplay } from './timestamp-display';
import { TimestampDisplayPipe } from './timestamp-display.pipe';

// 2026-06-19 16:00 America/New_York = 2026-06-19 20:00 UTC.
const EXPIRY_ANCHOR_MS = Date.UTC(2026, 5, 19, 20, 0, 0);

describe('formatTimestampDisplay', () => {
  it('formats local datetime with a pinned local zone', () => {
    expect(formatTimestampDisplay(EXPIRY_ANCHOR_MS, {
      mode: 'local',
      localTimeZone: 'America/New_York',
    })).toBe('2026-06-19 16:00:00');
  });

  it('formats exchange-aligned datetimes in ET with a marker', () => {
    expect(formatTimestampDisplay(EXPIRY_ANCHOR_MS, { mode: 'et' })).toBe(
      '2026-06-19 16:00:00 ET',
    );
  });

  it('keeps date-anchored values on their ET calendar date west of UTC', () => {
    expect(formatTimestampDisplay(EXPIRY_ANCHOR_MS, {
      mode: 'date-et',
      localTimeZone: 'America/Los_Angeles',
    })).toBe('2026-06-19');
  });

  it('uses 00 rather than 24 at midnight', () => {
    expect(formatTimestampDisplay(Date.UTC(2026, 0, 1, 0, 0, 0), {
      mode: 'local',
      localTimeZone: 'UTC',
    })).toBe('2026-01-01 00:00:00');
  });

  it('returns the fallback for absent values', () => {
    expect(formatTimestampDisplay(null, { mode: 'et' })).toBe('—');
  });
});

describe('TimestampDisplayPipe', () => {
  it('delegates inline template formatting to the shared core', () => {
    expect(new TimestampDisplayPipe().transform(EXPIRY_ANCHOR_MS, 'date-et')).toBe(
      '2026-06-19',
    );
  });
});

describe('TimestampDisplayComponent', () => {
  it('renders the formatted timestamp at the component seam', async () => {
    await render(TimestampDisplayComponent, {
      inputs: {
        value: EXPIRY_ANCHOR_MS,
        mode: 'et',
      },
    });

    expect(screen.getByText('2026-06-19 16:00:00 ET')).toBeTruthy();
  });

  it('supports projected prefix and suffix decoration', async () => {
    await render(
      `<app-timestamp-display [value]="value" mode="date-et">
        <span timestamp-prefix>expires </span>
        <span timestamp-suffix> NYSE</span>
      </app-timestamp-display>`,
      {
        imports: [TimestampDisplayComponent],
        componentProperties: { value: EXPIRY_ANCHOR_MS },
      },
    );

    const host = screen.getByText('2026-06-19').closest('app-timestamp-display');
    expect(host?.textContent?.replace(/\s+/g, ' ').trim()).toBe('expires 2026-06-19 NYSE');
  });
});
