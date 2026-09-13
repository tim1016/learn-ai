/** Canonical URL construction for clerk-scoped backend operations. */

import { ResourceTarget } from './resource-target';

type UrlSuffix = '' | `/${string}`;

function pathIdentity(value: string | null | undefined, name: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Cannot build a clerk-scoped URL without a ${name}.`);
  }
  return encodeURIComponent(value);
}

function clerkScope(target: Pick<ResourceTarget, 'broker' | 'clerkId'>): string {
  return `/api/brokers/${pathIdentity(target.broker, 'broker')}/clerks/${pathIdentity(
    target.clerkId,
    'clerk ID',
  )}`;
}

/** A lane-scoped path (reads and configuration families). */
export function laneUrl(
  target: Pick<ResourceTarget, 'broker' | 'clerkId'>,
  suffix: UrlSuffix = '',
): string {
  return `${clerkScope(target)}${suffix}`;
}

/** An account-scoped path. Empty account path segments are never serialized. */
export function accountUrl(
  target: Pick<ResourceTarget, 'broker' | 'clerkId' | 'accountId'>,
  suffix: UrlSuffix = '',
): string {
  return `${clerkScope(target)}/accounts/${pathIdentity(target.accountId, 'account ID')}${suffix}`;
}
