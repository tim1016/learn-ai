import type { ChartHistoryResponse, ChartOverlayNoticeView } from './broker-v2-panel.types';

/**
 * Closed set of history notice codes that describe a genuinely empty window
 * — Polygon answered and there is nothing to plot, not an outage (#2211).
 *
 * Classification is fail-loud by construction: a zero-bar response whose
 * code is anywhere outside this set — including `coordinator_unavailable`
 * (landing separately in #2204) or any other code this list has never
 * seen — is treated as unavailable, never as quiet "no candles". Only a
 * code proven to mean "the market really printed nothing" belongs here.
 *
 * Canonical implementation: this module. `build_history_chart`
 * (`PythonDataService/app/services/broker_v2_panel/chart_projection_service.py`)
 * is the only place `ChartHistoryResponse` is constructed, and it emits only
 * failure notices onto `overlay_notices` — a genuinely empty history window
 * arrives as an empty array, not a code. `polygon_overlay_empty` itself is
 * never emitted into HISTORY traffic; it is a LIVE-only code, emitted by
 * `app.services.live_chart_window` into the live chart response. It is kept
 * in this closed set as a forward-compatible guard, not because today's
 * history path can produce it.
 */
const HISTORY_EMPTY_NOTICE_CODES: ReadonlySet<string> = new Set(['polygon_overlay_empty']);

/**
 * The notice that makes a zero-bar history response *unavailable* rather
 * than genuinely empty — or `null` when the response has bars, carries no
 * notices, or every notice on it is in the closed empty-set above.
 *
 * Only the first qualifying notice is returned: one settled presentation
 * state needs at most one backend-authored message.
 */
export function historyUnavailableNotice(
  response: ChartHistoryResponse | null,
): ChartOverlayNoticeView | null {
  if (response === null || response.bars.length > 0) return null;
  const notices = response.overlay_notices ?? [];
  return notices.find((notice) => !HISTORY_EMPTY_NOTICE_CODES.has(notice.code)) ?? null;
}
