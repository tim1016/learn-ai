import { signal, type Signal } from '@angular/core';

/**
 * Lets one press of Run study start one run (PRD #3490 R05).
 *
 * A button's disabled state reaches the page only after the next render, so a
 * second click that lands first would start a second run. The gate closes
 * synchronously, in the handler, and opens again once the first press has
 * finished starting (the job accepted, the request settled). Pickers never go
 * through it: changing an input starts nothing.
 */
export interface RunGate {
  /** True from a press until that press has finished starting its run. */
  readonly starting: Signal<boolean>;
  /** Runs `start` unless an earlier press is still starting; a press during it is dropped. */
  press(start: () => Promise<void>): Promise<void>;
}

export function createRunGate(): RunGate {
  const starting = signal(false);
  return {
    starting: starting.asReadonly(),
    async press(start) {
      if (starting()) return;
      starting.set(true);
      try {
        await start();
      } finally {
        starting.set(false);
      }
    },
  };
}
