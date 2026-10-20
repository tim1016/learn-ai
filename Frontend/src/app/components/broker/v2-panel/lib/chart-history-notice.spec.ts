import { describe, expect, it } from 'vitest';

import type { ChartHistoryResponse, ChartOverlayNoticeView } from './broker-v2-panel.types';
import { historyUnavailableNotice } from './chart-history-notice';

function notice(code: string, message = 'A backend-authored notice.'): ChartOverlayNoticeView {
  return { code, message, source: 'polygon' };
}

function response(
  overrides: Partial<Pick<ChartHistoryResponse, 'bars' | 'overlay_notices'>> = {},
): ChartHistoryResponse {
  return {
    strategy_instance_id: 'sid-001',
    symbol: 'QQQ',
    timeframe: '1m',
    from_ms: 1_753_800_000_000,
    to_ms: 1_753_823_400_000,
    bars: [],
    indicator_bars: [],
    indicator_bar_budget: 0,
    indicator_bar_budget_satisfied: true,
    fill_markers: [],
    truncated: false,
    overlay_notices: [],
    as_of_ms: 1_753_800_000_000,
    ...overrides,
  };
}

describe('historyUnavailableNotice (#2211 fail-loud classification)', () => {
  it.each<[string, ChartHistoryResponse | null]>([
    ['for a null response', null],
    ['when the response has bars, regardless of notices', response({
      bars: [
        { start_ms: 1, end_ms: 2, open: '1', high: '1', low: '1', close: '1', volume: 1, source: 'polygon' },
      ],
      overlay_notices: [notice('polygon_api_key_missing')],
    })],
    ['for a zero-bar response with no notices (genuinely empty)', response({ overlay_notices: [] })],
    [
      'for a zero-bar response whose only notice is polygon_overlay_empty (the closed empty-set code)',
      response({ overlay_notices: [notice('polygon_overlay_empty')] }),
    ],
  ])('returns null %s', (_name, input) => {
    expect(historyUnavailableNotice(input)).toBeNull();
  });

  const missingKey = notice(
    'polygon_api_key_missing',
    'Polygon history is unavailable because POLYGON_API_KEY is not configured.',
  );
  const unseen = notice('coordinator_unavailable', 'The fleet coordinator did not respond in time.');
  const empty = notice('polygon_overlay_empty', 'Nothing printed for this session.');
  const outage = notice('polygon_rate_limited', 'Polygon rate-limited this request.');

  it.each<[string, ChartOverlayNoticeView[], ChartOverlayNoticeView]>([
    ['the notice for polygon_api_key_missing on a zero-bar response', [missingKey], missingKey],
    ['the notice for a code it has never seen, failing loud by default (coordinator_unavailable, #2204)', [unseen], unseen],
    ['the first non-empty-class notice when several are present', [empty, outage], outage],
  ])('returns %s', (_name, notices, expected) => {
    expect(historyUnavailableNotice(response({ overlay_notices: notices }))).toEqual(expected);
  });
});
