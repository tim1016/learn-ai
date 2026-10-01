/** Canonical URL construction for clerk-scoped backend operations. */

import { ResourceTarget } from './resource-target';

function pathIdentity(value: string | null | undefined, name: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Cannot build a clerk-scoped URL without a ${name}.`);
  }
  return encodeURIComponent(value);
}

/** The `/api/brokers/{broker}/clerks/{clerkId}` prefix every clerk-scoped
 * path shares — exported so `operation-url.ts` builds on it rather than
 * duplicating it. */
export function clerkScope(target: Pick<ResourceTarget, 'broker' | 'clerkId'>): string {
  return `/api/brokers/${pathIdentity(target.broker, 'broker')}/clerks/${pathIdentity(
    target.clerkId,
    'clerk ID',
  )}`;
}
