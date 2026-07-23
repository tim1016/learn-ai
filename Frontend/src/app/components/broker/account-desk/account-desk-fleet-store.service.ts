import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import type { FleetAccountSummary } from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';

interface FleetState {
  readonly summary: FleetAccountSummary | null;
  readonly loading: boolean;
  readonly errorMessage: string | null;
  readonly lastGoodAtMs: number | null;
}

const EMPTY_STATE: FleetState = {
  summary: null,
  loading: false,
  errorMessage: null,
  lastGoodAtMs: null,
};

/** Read-only fleet evidence that admits only a response attested to the route account. */
@Injectable()
export class AccountDeskFleetStore {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly destroyRef = inject(DestroyRef);
  private requestGeneration = 0;
  private readonly accountKey = signal<string | null>(null);
  private readonly state = signal<FleetState>(EMPTY_STATE);

  readonly summary = computed(() => this.state().summary);
  readonly loading = computed(() => this.state().loading);
  readonly errorMessage = computed(() => this.state().errorMessage);
  readonly hasLastGood = computed(() => this.state().summary !== null);
  readonly showingStaleLastGood = computed(() =>
    this.state().summary !== null && this.state().errorMessage !== null,
  );
  readonly lastGoodAtMs = computed(() => this.state().lastGoodAtMs);

  constructor() {
    this.destroyRef.onDestroy(() => { this.requestGeneration += 1; });
  }

  async load(accountId: string): Promise<void> {
    if (this.accountKey() !== accountId) {
      this.accountKey.set(accountId);
      this.state.set(EMPTY_STATE);
    }
    const generation = ++this.requestGeneration;
    this.state.update((state) => ({ ...state, loading: true, errorMessage: null }));
    try {
      const summary = await this.liveRuns.getAccountSummary(accountId);
      if (generation !== this.requestGeneration) return;
      if (summary.account_id !== accountId) {
        throw new Error('Fleet account evidence did not attest this route.');
      }
      this.state.set({ summary, loading: false, errorMessage: null, lastGoodAtMs: Date.now() });
    } catch (error) {
      if (generation !== this.requestGeneration) return;
      this.state.update((state) => ({
        ...state,
        loading: false,
        errorMessage: error instanceof Error ? error.message : 'Fleet evidence is unavailable. Retry to request it again.',
      }));
    }
  }

  retry(): void {
    const accountId = this.accountKey();
    if (accountId !== null) void this.load(accountId);
  }
}
