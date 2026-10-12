import { linkedSignal, type ResourceStatus, type Signal } from '@angular/core';

/** The answer to show for one question, and whether it is the answer of an earlier asking that is being read again. */
export interface Held<A> {
  readonly answer: A | null;
  readonly again: boolean;
}

interface Seen<A> extends Held<A> {
  readonly question: string | null;
}

/**
 * The last answer to a question, kept on screen while that same question is asked again, so that reading the day or
 * the chain once more after a download does not take the tables off the screen and put them back.
 *
 * Only the same question keeps its answer: a different date, expiry, time or strikes is a different question, and the
 * answer to the earlier one is never shown for it. A read that fails shows nothing held, and `question` null (no read
 * is asked for) holds nothing.
 */
export function heldAnswer<A>(question: () => string | null, status: () => ResourceStatus, answer: () => A | null): Signal<Held<A>> {
  return linkedSignal<{ question: string | null; status: ResourceStatus; answer: A | null }, Seen<A>>({
    source: () => ({ question: question(), status: status(), answer: answer() }),
    computation: (now, previous) => {
      if (now.status === 'resolved' || now.status === 'local') return { question: now.question, answer: now.answer, again: false };
      const earlier = previous?.value;
      const sameQuestion = now.question !== null && earlier !== undefined && earlier.question === now.question;
      if (now.status !== 'error' && sameQuestion && earlier.answer !== null) return { question: now.question, answer: earlier.answer, again: true };
      return { question: now.question, answer: now.status === 'reloading' ? now.answer : null, again: false };
    },
  });
}
