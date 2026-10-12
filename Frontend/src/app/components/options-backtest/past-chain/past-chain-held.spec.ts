import { signal, type ResourceStatus } from '@angular/core';
import { describe, expect, it } from 'vitest';

import { heldAnswer } from './past-chain-held';

/** The three things a read is: the question asked, how the read stands, and the answer it holds. */
function aRead() {
  const question = signal<string | null>('SPY|9');
  const status = signal<ResourceStatus>('idle');
  const answer = signal<string | null>(null);
  const held = heldAnswer<string>(question, status, answer);
  return { question, status, answer, held };
}

describe('the answer to show for a question that is asked again', () => {
  it('is the answer itself while it is read, and not an answer being read again', () => {
    const read = aRead();
    read.status.set('resolved');
    read.answer.set('first');

    expect(read.held()).toMatchObject({ answer: 'first', again: false });
  });

  it('is the last answer to the same question while it is read again, and says so', () => {
    const read = aRead();
    read.status.set('resolved');
    read.answer.set('first');
    expect(read.held().answer).toBe('first');

    read.status.set('loading');
    read.answer.set(null);

    expect(read.held()).toMatchObject({ answer: 'first', again: true });
  });

  it('is the last answer while the read waits on something else (idle), for the same question', () => {
    const read = aRead();
    read.status.set('resolved');
    read.answer.set('first');
    expect(read.held().answer).toBe('first');

    read.status.set('idle');
    read.answer.set(null);

    expect(read.held()).toMatchObject({ answer: 'first', again: true });
  });

  it('is the new answer once the read has it', () => {
    const read = aRead();
    read.status.set('resolved');
    read.answer.set('first');
    expect(read.held().answer).toBe('first');
    read.status.set('loading');
    read.answer.set(null);
    expect(read.held().again).toBe(true);

    read.status.set('resolved');
    read.answer.set('second');

    expect(read.held()).toMatchObject({ answer: 'second', again: false });
  });

  it('is nothing for another question, however the read stands: an answer is never shown for a question it was not the answer to', () => {
    const read = aRead();
    read.status.set('resolved');
    read.answer.set('first');
    expect(read.held().answer).toBe('first');

    read.question.set('SPY|10');
    read.status.set('loading');
    read.answer.set(null);

    expect(read.held()).toMatchObject({ answer: null, again: false });
  });

  it('does not bring back the answer to an earlier question when that question is asked again after another', () => {
    const read = aRead();
    read.status.set('resolved');
    read.answer.set('first');
    expect(read.held().answer).toBe('first');
    read.question.set('SPY|10');
    read.status.set('loading');
    read.answer.set(null);
    expect(read.held().answer).toBeNull();

    read.question.set('SPY|9');

    expect(read.held()).toMatchObject({ answer: null, again: false });
  });

  it('is nothing when the read failed, and nothing when no question is asked', () => {
    const read = aRead();
    read.status.set('resolved');
    read.answer.set('first');
    expect(read.held().answer).toBe('first');

    read.status.set('error');
    read.answer.set(null);
    expect(read.held()).toMatchObject({ answer: null, again: false });

    read.status.set('resolved');
    read.answer.set('first');
    expect(read.held().answer).toBe('first');
    read.question.set(null);
    read.status.set('idle');
    read.answer.set(null);
    expect(read.held()).toMatchObject({ answer: null, again: false });
  });

  it('is its own value while the read reloads and still has one', () => {
    const read = aRead();
    read.status.set('reloading');
    read.answer.set('kept');

    expect(read.held()).toMatchObject({ answer: 'kept', again: false });
  });
});
