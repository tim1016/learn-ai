import type { DestroyRef } from '@angular/core';

/** One re-armed timer for a record view that polls while its record is live; stops with the component. */
export class RecordPoller {
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(destroyRef: DestroyRef) {
    destroyRef.onDestroy(() => this.stop());
  }

  /** Arm one poll in `ms` (no-op for a non-positive interval, which tests use to disable polling). */
  schedule(ms: number, reload: () => void): void {
    this.stop();
    if (ms <= 0) return;
    this.timer = setTimeout(reload, ms);
  }

  stop(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}
