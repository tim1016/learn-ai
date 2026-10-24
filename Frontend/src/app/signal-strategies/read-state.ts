import { HttpErrorResponse } from '@angular/common/http';
import type { Resource } from '@angular/core';

import { pythonFailureMessage } from '../services/python-http-failure';
import { errorMessage } from '../shared/errors/error-message';

/**
 * Where one read stands, as a screen shows it: not asked yet or on its way, in
 * words it failed, or answered. A failed read is its own state because it is
 * not an empty answer: a section built on it says unknown, never none.
 */
export type ReadState<T> =
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed'; readonly message: string }
  | { readonly kind: 'ready'; readonly value: T };

export function readStateOf<T>(read: Resource<T | undefined>): ReadState<T> {
  const failure: unknown = read.error();
  if (failure !== undefined) {
    // Python's own sentence when it answered with one; the transport's words otherwise.
    return { kind: 'failed', message: failure instanceof HttpErrorResponse ? pythonFailureMessage(failure) : errorMessage(failure) };
  }
  const value = read.hasValue() ? read.value() : undefined;
  return value === undefined ? { kind: 'loading' } : { kind: 'ready', value };
}

/** A read that has nothing to wait for: the answer is already in hand. */
export const ALWAYS_READY: ReadState<true> = { kind: 'ready', value: true };

/**
 * Two reads as one: failed when either failed, loading until both are in,
 * ready with both answers. A section built on two reads is unknown while
 * either is.
 */
export function combineReads<A, B>(first: ReadState<A>, second: ReadState<B>): ReadState<readonly [A, B]> {
  if (first.kind === 'failed') return first;
  if (second.kind === 'failed') return second;
  if (first.kind === 'loading' || second.kind === 'loading') return { kind: 'loading' };
  return { kind: 'ready', value: [first.value, second.value] };
}
