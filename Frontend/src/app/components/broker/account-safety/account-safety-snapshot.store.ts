import { DestroyRef, Injectable, inject, signal, untracked } from '@angular/core';

import type { AccountSafetySnapshot } from '../../../api/broker-models';
import { BrokerService } from '../../../services/broker.service';

export type AccountSafetySnapshotLoadState =
  | 'loading'
  | 'fresh'
  | 'stale'
  | 'unavailable';

export interface AccountSafetySnapshotState {
  readonly state: AccountSafetySnapshotLoadState;
  readonly snapshot: AccountSafetySnapshot | null;
}

const UNAVAILABLE_STATE: AccountSafetySnapshotState = {
  state: 'unavailable',
  snapshot: null,
};

/**
 * Server-authored account safety snapshots, retained independently by account.
 *
 * A request generation is the total order for local refresh attempts and
 * fences transport reordering. Server milliseconds are evidence, not a client
 * version order: distinct snapshots may legitimately share a millisecond.
 * This store deliberately has no clean fallback: an unavailable fetch is either
 * stale last-good evidence or unavailable evidence.
 */
@Injectable({ providedIn: 'root' })
export class AccountSafetySnapshotStore {
  private readonly broker = inject(BrokerService);
  private readonly states = signal<ReadonlyMap<string, AccountSafetySnapshotState>>(new Map());
  private readonly requestGeneration = new Map<string, number>();

  stateFor(accountId: string | null): AccountSafetySnapshotState {
    if (accountId === null || accountId.trim() === '') return UNAVAILABLE_STATE;
    return this.states().get(accountId) ?? UNAVAILABLE_STATE;
  }

  async refresh(accountId: string): Promise<void> {
    const normalizedAccountId = accountId.trim();
    if (normalizedAccountId === '') return;
    const generation = untracked(() => {
      const nextGeneration = (this.requestGeneration.get(normalizedAccountId) ?? 0) + 1;
      this.requestGeneration.set(normalizedAccountId, nextGeneration);
      const previous = this.states().get(normalizedAccountId) ?? UNAVAILABLE_STATE;
      this.setState(normalizedAccountId, {
        state: previous.snapshot === null ? 'loading' : 'stale',
        snapshot: previous.snapshot,
      });
      return nextGeneration;
    });

    try {
      const snapshot = await this.broker.accountSafetySnapshot(normalizedAccountId);
      if (this.requestGeneration.get(normalizedAccountId) !== generation) return;
      const current = this.states().get(normalizedAccountId) ?? UNAVAILABLE_STATE;
      if (snapshot.account_id !== normalizedAccountId) {
        this.setState(normalizedAccountId, {
          state: current.snapshot === null ? 'unavailable' : 'stale',
          snapshot: current.snapshot,
        });
        return;
      }
      this.setState(normalizedAccountId, { state: 'fresh', snapshot });
    } catch {
      if (this.requestGeneration.get(normalizedAccountId) !== generation) return;
      const current = this.states().get(normalizedAccountId) ?? UNAVAILABLE_STATE;
      this.setState(normalizedAccountId, {
        state: current.snapshot === null ? 'unavailable' : 'stale',
        snapshot: current.snapshot,
      });
    }
  }

  private setState(accountId: string, next: AccountSafetySnapshotState): void {
    this.states.update((states) => new Map(states).set(accountId, next));
  }
}

/** Explicitly local browser transport state. It is never broker proof. */
@Injectable({ providedIn: 'root' })
export class BrowserLocalEvidenceService {
  private readonly destroyRef = inject(DestroyRef);
  readonly online = signal(typeof navigator === 'undefined' ? true : navigator.onLine);

  constructor() {
    if (typeof window === 'undefined') return;
    const markOnline = () => this.online.set(true);
    const markOffline = () => this.online.set(false);
    window.addEventListener('online', markOnline);
    window.addEventListener('offline', markOffline);
    this.destroyRef.onDestroy(() => {
      window.removeEventListener('online', markOnline);
      window.removeEventListener('offline', markOffline);
    });
  }
}
