import { Injectable, signal } from '@angular/core';

import type { OperatorBlocker } from '../api/operator-blocker.types';

export type ShellBrokerId = 'alpaca' | 'ibkr';
export type ShellAccountMode = 'paper' | 'live';

/** The account identity that contextual shell chrome may present. */
export interface ShellBrokerAccount {
  readonly broker: ShellBrokerId;
  /** Kept for route construction; shell UI must never render it. */
  readonly accountId: string;
  readonly accountMode: ShellAccountMode;
}

/**
 * Contract for the account-scoped section of the application shell.
 *
 * This deliberately starts empty. The router-driven live implementation lands
 * separately, while shell consumers can already depend on one stable shape.
 */
@Injectable({ providedIn: 'root' })
export class ShellAccountContextService {
  readonly brokerAccount = signal<ShellBrokerAccount | null>(null);
  readonly botsTrading = signal(0);
  readonly fills = signal(0);
  readonly attentionConditions = signal<readonly OperatorBlocker[]>([]);
}
