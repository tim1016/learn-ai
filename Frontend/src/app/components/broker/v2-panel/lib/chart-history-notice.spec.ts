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
  it('returns null for a null response', () => {
    expect(historyUnavailableNotice(null)).toBeNull();
  });

  it('returns null when the response has bars, regardless of notices', () => {
    const withBars = response({
      bars: [
        { start_ms: 1, end_ms: 2, open: '1', high: '1', low: '1', close: '1', volume: 1, source: 'polygon' },
      ],
      overlay_notices: [notice('polygon_api_key_missing')],
    });
    expect(historyUnavailableNotice(withBars)).toBeNull();
  });

  it('returns null for a zero-bar response with no notices (genuinely empty)', () => {
    expect(historyUnavailableNotice(response({ overlay_notices: [] }))).toBeNull();
  });

  it('returns null for a zero-bar response whose only notice is polygon_overlay_empty (the closed empty-set code)', () => {
    expect(
      historyUnavailableNotice(response({ overlay_notices: [notice('polygon_overlay_empty')] })),
    ).toBeNull();
  });

  it('returns the notice for polygon_api_key_missing on a zero-bar response', () => {
    const missingKey = notice(
      'polygon_api_key_missing',
      'Polygon history is unavailable because POLYGON_API_KEY is not configured.',
    );
    expect(historyUnavailableNotice(response({ overlay_notices: [missingKey] }))).toEqual(missingKey);
  });

  it('defaults fail-loud for a code it has never seen (coordinator_unavailable, #2204)', () => {
    const unseen = notice('coordinator_unavailable', 'The fleet coordinator did not respond in time.');
    expect(historyUnavailableNotice(response({ overlay_notices: [unseen] }))).toEqual(unseen);
  });

  it('returns the first non-empty-class notice when several are present', () => {
    const empty = notice('polygon_overlay_empty', 'Nothing printed for this session.');
    const outage = notice('polygon_rate_limited', 'Polygon rate-limited this request.');
    expect(historyUnavailableNotice(response({ overlay_notices: [empty, outage] }))).toEqual(outage);
  });
});
