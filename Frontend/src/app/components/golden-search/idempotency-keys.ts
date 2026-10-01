/**
 * One idempotency key per intended request (#2696). Retrying the very same
 * request after an unanswered attempt (a lost response) reuses its key, so the
 * server replays the stored outcome instead of acting twice; any other request
 * gets a fresh key. `settle` forgets the key once the server has answered.
 */
export class IdempotencyKeys {
  private pending: { readonly fingerprint: string; readonly key: string } | null = null;

  constructor(private readonly newKey: () => string = () => globalThis.crypto.randomUUID()) {}

  keyFor(fingerprint: string): string {
    if (this.pending?.fingerprint !== fingerprint) this.pending = { fingerprint, key: this.newKey() };
    return this.pending.key;
  }

  settle(): void {
    this.pending = null;
  }
}
