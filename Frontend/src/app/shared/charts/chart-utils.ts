import { UTCTimestamp } from 'lightweight-charts';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * Custom tick mark formatter for lightweight-charts.
 * Shows "Mon DD" for day ticks instead of just "DD".
 *
 * tickMarkType values (TickMarkType enum):
 *   0 = Year, 1 = Month, 2 = DayOfMonth, 3 = Time, 4 = TimeWithSeconds
 */
export function formatTickMark(
  time: UTCTimestamp,
  tickMarkType: number,
  _locale: string
): string {
  const d = new Date((time as number) * 1000);

  switch (tickMarkType) {
    case 0: // Year
      return d.getUTCFullYear().toString();
    case 1: // Month
      return `${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
    case 2: // DayOfMonth
      return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
    case 3: // Time
      return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    case 4: // TimeWithSeconds
      return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
    default:
      return '';
  }
}

function pad(n: number): string {
  return n.toString().padStart(2, '0');
}
