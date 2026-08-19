/**
 * Contract types for the broker-v2 bot gallery aggregated stream.
 *
 * `GalleryPrimaryAction`, `GalleryBotView`, `GallerySymbolBars`, and
 * `GalleryLiveSnapshot` alias the generated OpenAPI schemas below — the
 * snapshot doubles as the REST bootstrap response and the SSE `snapshot`
 * event payload, so it is not an OpenAPI exception. `GalleryLiveUpdate` and
 * `GalleryResetEvent` are genuinely stream-only (no REST response uses
 * them) and stay hand-declared, each pinned to its backend authority — see
 * the comment on each. `ChartBar` and `ChartFillMarker` are reused from the
 * sibling panel types rather than redefined, per the
 * single-canonical-implementation rule.
 *
 * Temporal fields are `int64 ms UTC` per `.claude/rules/temporal-rigor.md`.
 */
import type { components } from '../../../../../api/broker.types';
import type { ChartBar, ChartFillMarker } from '../../lib/broker-v2-panel.types';

export type { ChartBar, ChartFillMarker };

export type GalleryResolution = components['schemas']['GalleryLiveSnapshot']['resolution'];

// Fields the backend gives a Pydantic default (`= None` /
// `Field(default_factory=...)`) come through as optional (`?:`) in the
// generated schema even though every response serializes them — treat
// absence the same as the backend's own default (`?? null` / `?? []` /
// `?? {}`) at the few call sites that read these keys, rather than
// asserting a stronger type than the wire contract actually promises.
export type GalleryPrimaryAction = components['schemas']['GalleryPrimaryAction'];

/** One bot's tile state in the gallery wall. */
export type GalleryBotView = components['schemas']['GalleryBotView'];

/** Bars for one symbol, shared across every tile that charts it. */
export type GallerySymbolBars = components['schemas']['GallerySymbolBars'];

/** Versioned complete state document — the REST bootstrap/poll-fallback body and the SSE `snapshot` event payload. */
export type GalleryLiveSnapshot = components['schemas']['GalleryLiveSnapshot'];

/**
 * Incremental gallery update carried over the SSE `update` event.
 *
 * SSE-only — no REST response returns this shape, so there is no generated
 * OpenAPI schema to alias. Pinned instead to its backend owner,
 * `app.schemas.broker_v2_gallery.GalleryLiveUpdate`
 * (`tests/schemas/test_broker_v2_gallery.py::test_live_update_fields_match_the_pinned_frontend_type`
 * fails if the two field sets diverge).
 */
export interface GalleryLiveUpdate {
  readonly surface_version: number;
  readonly as_of_ms: number;
  readonly symbols: readonly GallerySymbolBars[];
  readonly markers_delta: Readonly<Record<string, readonly ChartFillMarker[]>>;
  readonly bots_delta: readonly GalleryBotView[];
  readonly removed_sids: readonly string[];
}

/**
 * The SSE `reset` event payload — the epoch changed; re-bootstrap.
 *
 * No Pydantic model backs this payload; `broker_v2_gallery.py`'s router
 * constructs it inline (`json.dumps({"reason": ..., "cursor": ...})`) and is
 * the sole authority for its shape. Pinned by
 * `tests/routers/test_broker_v2_gallery.py::test_gallery_stream_stale_cursor_emits_reset_first`,
 * which asserts the emitted payload's key set is exactly `{reason, cursor}`.
 */
export interface GalleryResetEvent {
  readonly reason: string;
  readonly cursor: string;
}

export type GalleryLiveStatus = 'connecting' | 'live' | 'stale' | 'error';
