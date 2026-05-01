import { Signal, signal } from '@angular/core';

/**
 * One line in a job's progress log.
 *
 * The shape is shared across feature-runner, signal-engine, and
 * cross-sectional. Anything job-specific (chunk index, ticker symbol,
 * etc.) goes into `message` as pre-formatted text — the panel just
 * prints it. The level drives the colour stripe.
 */
export interface RunLogEntry {
  /** Stable key for `@for ... track`. Unique within the buffer. */
  id: string;
  /** Wall-clock millis when the entry was appended. */
  timestamp: number;
  /** Severity drives the colour stripe in the panel. */
  level: 'info' | 'success' | 'warn' | 'error';
  /** Single-character glyph rendered before the message. */
  glyph: string;
  /** Single-line summary. Pre-formatted; the panel just prints it. */
  message: string;
}

/**
 * Default cap for the rolling FIFO log buffer. Tuned high enough that one
 * heavy run can fit its lifecycle without rolling earlier entries off
 * screen, low enough to keep change detection fast.
 *
 * The user explicitly chose 500 — when the buffer exceeds the cap we
 * drop the oldest entries silently (no "X older entries" placeholder).
 */
export const RUN_LOG_DEFAULT_CAP = 500;

/**
 * Signal-backed FIFO log buffer.
 *
 * Used by every runner UI (feature-research, signal-engine, cross-
 * sectional batch). The panel binds directly to ``entries()`` for live
 * scrolling.
 *
 * Append cost is O(1) until the cap is hit, then O(n) once per append
 * (slice). Cap is configurable but defaults to 500.
 */
export class RunLogBuffer {
  private readonly _entries = signal<readonly RunLogEntry[]>([]);
  private _seq = 0;

  constructor(private readonly cap: number = RUN_LOG_DEFAULT_CAP) {}

  readonly entries: Signal<readonly RunLogEntry[]> = this._entries.asReadonly();

  /** Push one entry. Drops oldest if over cap. */
  append(level: RunLogEntry['level'], glyph: string, message: string): void {
    const entry: RunLogEntry = {
      id: `${Date.now()}-${++this._seq}`,
      timestamp: Date.now(),
      level,
      glyph,
      message,
    };
    this._entries.update((current) => {
      const next = [...current, entry];
      return next.length > this.cap ? next.slice(-this.cap) : next;
    });
  }

  /** Drop everything. */
  clear(): void {
    this._entries.set([]);
  }

  /** Current length. Useful for tests. */
  size(): number {
    return this._entries().length;
  }
}

/**
 * Map a Python logger level to a `RunLogEntry.level`.
 *
 * Centralised so feature/signal/cross-sectional runners all classify
 * their log lines the same way.
 */
export function pythonLevelToEntryLevel(
  level: string | undefined,
): RunLogEntry['level'] {
  const normalized = (level ?? 'info').toLowerCase();
  if (normalized === 'error' || normalized === 'critical') return 'error';
  if (normalized === 'warn' || normalized === 'warning') return 'warn';
  if (normalized === 'success') return 'success';
  return 'info';
}

/** The conventional glyph for a level. */
export function glyphForLevel(level: RunLogEntry['level']): string {
  switch (level) {
    case 'error':
      return '✗';
    case 'warn':
      return '⚠';
    case 'success':
      return '✓';
    default:
      return 'ⓘ';
  }
}
