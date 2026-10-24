import { computed, signal, type Signal } from '@angular/core';

/** One input as the reader knows it: "Symbol" → "SPY". */
export interface ResultInputFact {
  readonly label: string;
  readonly value: string;
}

/** The inputs a shown result answers, captured when its request was submitted. */
export interface ResultInputs {
  /**
   * Identity of the complete input set: two inputs with the same key ask the
   * same question. A host whose inputs can be invalid (half a date, a
   * reversed range) gives an invalid set a key of its own, so an invalid edit
   * marks the shown result as previous just as a valid one does.
   */
  readonly key: string;
  /** The inputs in the words the reader uses, for display beside the result. */
  readonly facts: readonly ResultInputFact[];
}

/**
 * Tracks which inputs a shown result answers (PRD #3490 R04).
 *
 * A result is current only while the inputs now entered are the inputs it was
 * produced for. The moment they differ it is a previous result: it may stay
 * on screen, marked with its captured inputs, but it no longer answers the
 * form and nothing may consume it.
 */
export interface ResultInputTracker<Inputs> {
  /** The inputs the shown result answers, or `null` when none is shown. */
  readonly captured: Signal<ResultInputs | null>;
  /** A result is shown and the inputs now entered differ from its inputs. */
  readonly isPrevious: Signal<boolean>;
  /** A result is shown and answers the inputs now entered. The gate for
   * approving, deploying or otherwise using it. */
  readonly answersCurrentInputs: Signal<boolean>;
  /** Records the inputs a result was *submitted* with, when it lands. Pass the
   * request's own inputs, never the form's now: it may have changed while the
   * work ran, and the result answers what was asked. */
  capture(inputs: Inputs): void;
  /** Forgets the shown result. */
  clear(): void;
}

/**
 * @param current the inputs now entered, as a signal the host already holds.
 * @param describe turns a set of inputs into its identity and its display facts.
 */
export function createResultInputTracker<Inputs>(
  current: Signal<Inputs>,
  describe: (inputs: Inputs) => ResultInputs,
): ResultInputTracker<Inputs> {
  const captured = signal<ResultInputs | null>(null);
  const currentKey = computed(() => describe(current()).key);
  const matchesCurrent = computed(() => {
    const result = captured();
    return result === null ? null : result.key === currentKey();
  });

  return {
    captured: captured.asReadonly(),
    isPrevious: computed(() => matchesCurrent() === false),
    answersCurrentInputs: computed(() => matchesCurrent() === true),
    capture: (inputs) => captured.set(describe(inputs)),
    clear: () => captured.set(null),
  };
}
