import { provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { AccountTriageResponse } from '../../../api/account-reconciliation.types';
import type { OperatorBlocker } from '../../../api/operator-blocker.types';
import { AccountDeskHoldingsStore } from './account-desk-holdings-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';
import { AccountDeskGuidanceStore } from './account-desk-guidance-store.service';

describe('AccountDeskGuidanceStore', () => {
  const triage = signal<AccountTriageResponse | null>(null);
  const holdingsBlockers = signal<readonly OperatorBlocker[]>([]);

  beforeEach(() => {
    triage.set(null);
    holdingsBlockers.set([]);
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskGuidanceStore,
        { provide: AccountDeskSurfaceStore, useValue: { triage } },
        { provide: AccountDeskHoldingsStore, useValue: { operatorBlockers: holdingsBlockers } },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('falls unknown future anchors back to verdict and keeps audience as presentation only', () => {
    const futureAnchor = {
      ...blocker('future-anchor', 'both', 'surface'),
      anchor: { kind: 'a-new-anchor', subject_key: null },
    } as unknown as OperatorBlocker;
    triage.set({ operator_blockers: [futureAnchor, blocker('operator-only', 'operator', 'lease')] } as AccountTriageResponse);
    const store = TestBed.inject(AccountDeskGuidanceStore);

    expect(store.blockersFor('verdict', null, 'trader').map((value) => value.condition.id)).toEqual(['future-anchor']);
    expect(store.blockersFor('lease', null, 'trader')).toEqual([]);
    expect(store.blockersFor('lease', null, 'operator').map((value) => value.condition.id)).toEqual(['operator-only']);
    expect(store.blockersFor('verdict', null, 'operator')[0]?.headline).toBe('future-anchor headline');
  });

  it('deduplicates same-condition projections and counts operator attention once', () => {
    triage.set({ operator_blockers: [blocker('same-condition', 'operator', 'reconciliation')] } as AccountTriageResponse);
    holdingsBlockers.set([blocker('same-condition', 'operator', 'reconciliation')]);
    const store = TestBed.inject(AccountDeskGuidanceStore);

    expect(store.blockersFor('reconciliation', null, 'operator')).toHaveLength(1);
    expect(store.operatorAttentionCount()).toBe(1);
  });
});

function blocker(
  id: string,
  audience: OperatorBlocker['audience'],
  anchor: OperatorBlocker['anchor']['kind'],
): OperatorBlocker {
  return {
    condition: { id, severity: 'blocking', scope: 'account', evidence: {} },
    host: 'account_desk',
    anchor: { kind: anchor, subject_key: null },
    audience,
    disposition: 'wait',
    headline: `${id} headline`,
    detail: `${id} detail`,
    primary_move: null,
    secondary_moves: [],
    applies_to: 'both',
  };
}
