import { computed, signal } from '@angular/core';

/**
 * The name a run is recorded under. A run is recorded under the name of the setup that was opened or last saved, but
 * only while the setup on screen is still that one: once it is edited it is another setup, and a run of it carries no
 * name. Every way the form is replaced says so here: `set` for a setup that was opened or saved, and `clear` for one
 * that has no name, so that the rule lives in one place and no way of replacing the form can forget half of it.
 */
export class RunNameTracker {
  private readonly loaded = signal<{ readonly name: string; readonly body: string | null } | null>(null);
  private replaced = 0;

  /** The name of the setup that was opened or last saved, edited since or not; the side list marks it. */
  readonly name = computed(() => this.loaded()?.name ?? null);

  /** How many times the form has been replaced whole, so that a save which was under way can tell that another setup came in meanwhile. */
  get replacements(): number {
    return this.replaced;
  }

  /** A setup was opened or saved under this name; `body` is the request body of it as JSON. */
  set(name: string, body: string | null): void {
    this.loaded.set({ name, body });
    this.replaced += 1;
  }

  /** The form was replaced by a setup that has no name, or the setup that was named is gone. */
  clear(): void {
    this.loaded.set(null);
    this.replaced += 1;
  }

  /** The name a run is recorded under for the setup whose request body is this JSON, or null when it is no longer the one that was named. */
  nameFor(body: string | null): string | null {
    const loaded = this.loaded();
    return loaded !== null && loaded.body === body ? loaded.name : null;
  }
}
