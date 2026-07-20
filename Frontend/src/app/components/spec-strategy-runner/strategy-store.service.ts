import { Injectable, signal } from '@angular/core';
import { StrategySpec } from '../../graphql/spec-strategy.models';

/**
 * One saved strategy entry.
 *
 * Keep the wire shape stable — when we move from localStorage to a
 * server-backed multi-user store, the same shape is what the API will
 * round-trip. The ``id`` is generated on save and never changes; the
 * ``name`` is user-editable.
 */
export interface SavedStrategy {
  /** Stable id, generated on first save. UUID-like but uniqueness only
   * needs to hold within one user's storage. */
  readonly id: string;
  /** User-facing name. Free text. */
  readonly name: string;
  /** Last modified timestamp as int64 ms UTC. */
  readonly updatedAt: number;
  /** First-saved timestamp as int64 ms UTC. */
  readonly createdAt: number;
  /** The full StrategySpec at save time. */
  readonly spec: StrategySpec;
}

const LOCAL_STORAGE_KEY = 'learn-ai.spec-strategy.saved.v1';

/**
 * Strategy persistence abstraction.
 *
 * Phase 1: localStorage (this file). Phase 2: server-backed via a new
 * GraphQL mutation, behind the same interface so the component
 * doesn't change. The component depends on this service, never on
 * ``localStorage`` directly.
 *
 * Multi-user semantics are NOT in scope yet — the localStorage layer
 * stores per-browser, per-user-isn't-a-concept. When DB persistence
 * lands, this service grows a "current user id" awareness; the
 * interface stays the same.
 */
@Injectable({ providedIn: 'root' })
export class SpecStrategyStore {
  /** Reactive snapshot of the saved-strategy list. UI binds to this. */
  private readonly _entries = signal<SavedStrategy[]>(this.loadFromStorage());
  readonly entries = this._entries.asReadonly();

  /** Save a new strategy or overwrite an existing one (matched by id). */
  save(name: string, spec: StrategySpec, existingId?: string): SavedStrategy {
    const now = Date.now();
    const list = [...this._entries()];
    if (existingId) {
      const idx = list.findIndex((e) => e.id === existingId);
      if (idx >= 0) {
        const updated: SavedStrategy = {
          ...list[idx],
          name,
          spec,
          updatedAt: now,
        };
        list[idx] = updated;
        this.commit(list);
        return updated;
      }
    }
    const created: SavedStrategy = {
      id: this.generateId(),
      name,
      createdAt: now,
      updatedAt: now,
      spec,
    };
    list.push(created);
    this.commit(list);
    return created;
  }

  /** Rename in place. No-op if id not found. */
  rename(id: string, name: string): void {
    const list = [...this._entries()];
    const idx = list.findIndex((e) => e.id === id);
    if (idx < 0) return;
    list[idx] = { ...list[idx], name, updatedAt: Date.now() };
    this.commit(list);
  }

  /** Delete by id. No-op if not found. */
  remove(id: string): void {
    const list = this._entries().filter((e) => e.id !== id);
    this.commit(list);
  }

  /** Look up by id. Returns ``undefined`` if not found. */
  getById(id: string): SavedStrategy | undefined {
    return this._entries().find((e) => e.id === id);
  }

  /** Clone an existing strategy under a new name; the clone gets a fresh id. */
  clone(sourceId: string, newName: string): SavedStrategy | undefined {
    const source = this.getById(sourceId);
    if (!source) return undefined;
    return this.save(newName, source.spec);
  }

  // ---- Internals -------------------------------------------------------
  private commit(list: SavedStrategy[]): void {
    // Sort by updatedAt desc so the most-recent saves bubble to the top.
    const sorted = [...list].sort((a, b) => b.updatedAt - a.updatedAt);
    this._entries.set(sorted);
    try {
      localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(sorted));
    } catch {
      // Storage may be disabled (private mode, quota). The in-memory
      // signal still reflects the change for the duration of the
      // session — silent failure is preferable to crashing the UI on a
      // save action.
    }
  }

  private loadFromStorage(): SavedStrategy[] {
    if (typeof localStorage === 'undefined') return [];
    try {
      const raw = localStorage.getItem(LOCAL_STORAGE_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw) as unknown;
      if (!Array.isArray(parsed)) return [];
      // Light defensive shape check — corrupt entries are skipped, not
      // thrown on. The store always boots even if storage is malformed.
      return parsed.filter(
        (e): e is SavedStrategy =>
          !!e &&
          typeof e === 'object' &&
          typeof (e as { id?: unknown }).id === 'string' &&
          typeof (e as { name?: unknown }).name === 'string' &&
          typeof (e as { spec?: unknown }).spec === 'object',
      );
    } catch {
      return [];
    }
  }

  private generateId(): string {
    // crypto.randomUUID is widely available in modern browsers and the
    // tested Vitest jsdom environment. Fallback to a timestamp-suffixed
    // random string for older runtimes — collisions within one user's
    // storage are extremely unlikely.
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    return `s_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
  }
}
