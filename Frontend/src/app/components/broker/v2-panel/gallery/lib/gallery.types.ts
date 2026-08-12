/**
 * Contract types for the broker-v2 bot gallery aggregated stream.
 *
 * Mirrors the backend Pydantic schemas in
 * `PythonDataService/app/schemas/broker_v2_gallery.py` field-for-field
 * (snake_case is correct here — it is the wire shape). `ChartBar` and
 * `ChartFillMarker` are reused from the sibling panel types rather than
 * redefined, per the single-canonical-implementation rule.
 *
 * Temporal fields are `int64 ms UTC` per `.claude/rules/temporal-rigor.md`.
 */
import type { ChartBar, ChartFillMarker } from '../../lib/broker-v2-panel.types';

export type { ChartBar, ChartFillMarker };

export interface GalleryPrimaryAction {
  readonly action_id: string;
  readonly label: string;
  readonly enabled: boolean;
  readonly disabled_reason: string | null;
}

/** One bot's tile state in the gallery wall. */
export interface GalleryBotView {
  readonly sid: string;
  readonly symbol: string;
  readonly label: string;
  readonly running: boolean;
  readonly phase: string;
  readonly desired_state: string;
  readonly needs_attention: boolean;
  readonly realized_pnl_today: number | null;
  readonly open_pnl: number | null;
  readonly fills_today: number | null;
  readonly last_bar_at_ms: number | null;
  readonly primary_action: GalleryPrimaryAction;
}

/** Bars for one symbol, shared across every tile that charts it. */
export interface GallerySymbolBars {
  readonly symbol: string;
  readonly bars: readonly ChartBar[];
}

/** Versioned complete state document — the REST bootstrap/poll-fallback body and the SSE `snapshot` event payload. */
export interface GalleryLiveSnapshot {
  readonly stream_epoch: string;
  readonly surface_version: number;
  readonly as_of_ms: number;
  readonly resolution: string;
  readonly bots: readonly GalleryBotView[];
  readonly symbols: readonly GallerySymbolBars[];
  readonly markers: Readonly<Record<string, readonly ChartFillMarker[]>>;
}

/** Incremental gallery update carried over the SSE `update` event. */
export interface GalleryLiveUpdate {
  readonly surface_version: number;
  readonly as_of_ms: number;
  readonly symbols: readonly GallerySymbolBars[];
  readonly markers_delta: Readonly<Record<string, readonly ChartFillMarker[]>>;
  readonly bots_delta: readonly GalleryBotView[];
  readonly removed_sids: readonly string[];
}

/** The SSE `reset` event payload — the epoch changed; re-bootstrap. */
export interface GalleryResetEvent {
  readonly reason: string;
  readonly cursor: string;
}

export type GalleryLiveStatus = 'connecting' | 'live' | 'stale' | 'error';
