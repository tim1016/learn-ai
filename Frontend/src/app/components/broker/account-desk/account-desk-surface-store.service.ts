import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import type { AccountTriageResponse } from '../../../api/account-reconciliation.types';
import { BrokerService } from '../../../services/broker.service';

/** Route-scoped Account desk projection keyed only by the current account route parameter. */
@Injectable()
export class AccountDeskSurfaceStore {
  private readonly broker = inject(BrokerService);
  private readonly destroyRef = inject(DestroyRef);
  private requestGeneration = 0;

  private readonly accountKey = signal<string | null>(null);
  private readonly triageState = signal<AccountTriageResponse | null>(null);
  private readonly loadingState = signal(false);
  private readonly errorState = signal<unknown>(null);

  readonly accountId = this.accountKey.asReadonly();
  readonly triage = this.triageState.asReadonly();
  readonly loading = this.loadingState.asReadonly();
  readonly error = this.errorState.asReadonly();
  readonly showingStaleLastGood = computed(() => this.triageState() !== null && this.errorState() !== null);

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.requestGeneration += 1;
    });
  }

  async load(accountId: string): Promise<void> {
    if (this.accountKey() !== accountId) {
      this.accountKey.set(accountId);
      this.triageState.set(null);
      this.errorState.set(null);
    }
    const generation = ++this.requestGeneration;
    this.loadingState.set(true);
    this.errorState.set(null);
    try {
      const triage = await this.broker.accountTriage(accountId);
      if (generation !== this.requestGeneration) return;
      this.triageState.set(triage);
    } catch (error) {
      if (generation !== this.requestGeneration) return;
      this.errorState.set(error);
    } finally {
      if (generation === this.requestGeneration) this.loadingState.set(false);
    }
  }

  retry(): void {
    const accountId = this.accountKey();
    if (accountId) void this.load(accountId);
  }
}
