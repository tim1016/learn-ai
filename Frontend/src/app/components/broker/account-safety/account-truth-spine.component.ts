import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { CardModule } from 'primeng/card';
import { TagModule } from 'primeng/tag';

import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import {
  BrowserLocalEvidenceService,
  type AccountSafetySnapshotState,
} from './account-safety-snapshot.store';
import { PresentedAccountActionComponent } from './presented-account-action.component';

/** Compact, server-authored account-safety spine shared across operator hosts. */
@Component({
  selector: 'app-account-truth-spine',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CardModule, PresentedAccountActionComponent, ReceiptLabelPipe, TagModule, TimestampDisplayComponent],
  templateUrl: './account-truth-spine.component.html',
  styleUrl: './account-truth-spine.component.scss',
})
export class AccountTruthSpineComponent {
  private readonly browserEvidence = inject(BrowserLocalEvidenceService);
  readonly accountState = input.required<AccountSafetySnapshotState>();
  readonly browserOnline = this.browserEvidence.online;
  readonly snapshot = computed(() => this.accountState().snapshot);
  readonly primaryBlocker = computed(() => this.snapshot()?.blockers?.[0] ?? null);
  readonly presentedActions = computed(() => this.snapshot()?.actions ?? []);
  readonly sourceNeedingAttention = computed(
    () => this.snapshot()?.sources.find((source) => source.state !== 'FRESH') ?? null,
  );
  readonly verdictSeverity = computed<'success' | 'info' | 'warn' | 'danger'>(() => {
    switch (this.snapshot()?.verdict) {
      case 'CLEAN': return 'success';
      case 'RECONCILING': return 'info';
      case 'STALE': return 'warn';
      case 'SUSPENDED':
      case 'CONTAMINATED':
      case 'UNAVAILABLE': return 'danger';
      default: return 'danger';
    }
  });
}
