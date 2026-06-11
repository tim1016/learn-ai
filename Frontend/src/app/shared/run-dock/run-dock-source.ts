import { InjectionToken, Signal } from '@angular/core';

/**
 * Coarse state the dock cares about. Each surface maps its own internal
 * state machine (e.g. data-lab's fetching/bundling/done/error, engine-lab's
 * queued/running/completed/failed/cancelled) onto this small enum so the
 * dock can render a consistent collapsed-strip styling regardless of which
 * lab is hosting it.
 */
export type RunDockState = 'idle' | 'active' | 'done' | 'error';

export type RunDockLevel = 'info' | 'success' | 'warn' | 'error';

/** A single line in the dock's scrolling event log. */
export interface RunLogEntry {
  /** Monotonic id so two entries in the same millisecond stay distinct
   *  under `@for` track expressions. */
  id: string;
  /** Wall-clock millis when the entry was appended. */
  timestamp: number;
  /** Severity drives the colour stripe in the dock. */
  level: RunDockLevel;
  /** Single-character glyph rendered before the message. */
  glyph: string;
  /** Single-line pre-formatted message. */
  message: string;
}

/**
 * Contract a surface (data-lab, engine-lab) provides to the shared
 * `RunDockComponent`. Each surface implements this with its own state
 * machine but the dock only sees the generic shape.
 */
export interface RunDockSource {
  dockState: Signal<RunDockState>;
  headline: Signal<string>;
  headlineLevel: Signal<RunDockLevel>;
  /** Whole-percent, or `null` when no determinate progress is available
   *  (e.g. an opaque sidecar phase that only reports elapsed time). */
  progressPercent: Signal<number | null>;
  /** Pre-formatted ETA string, or `null` when too early to estimate. */
  etaText: Signal<string | null>;
  canCancel: Signal<boolean>;
  log: Signal<readonly RunLogEntry[]>;
  clearLog(): void;
  cancel(): Promise<void> | void;
}

/**
 * DI token through which the dock receives its source. Each lab page
 * provides this at its component-level injector with its own
 * implementation (RunSessionService for data-lab, EngineRunDockSource
 * for engine-lab).
 */
export const RUN_DOCK_SOURCE = new InjectionToken<RunDockSource>('RUN_DOCK_SOURCE');

/**
 * Per-surface localStorage key for the dock's expand/collapse persistence.
 * The default works for callers that don't care about per-surface state,
 * but each lab page should override with a unique key so its expanded
 * state doesn't leak across labs.
 */
export const RUN_DOCK_STORAGE_KEY = new InjectionToken<string>('RUN_DOCK_STORAGE_KEY', {
  factory: () => 'run-dock-expanded',
});
