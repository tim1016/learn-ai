import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from '@angular/core';

import type {
  CohortBatchLaunchMemberOutcome,
  CohortBatchLaunchStatus,
  CohortEvidenceMember,
} from '../../../api/cohort-batch-launch.types';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { LiveRunsService } from '../../../services/live-runs.service';

interface CohortMonitorRow {
  strategyInstanceId: string;
  outcome: CohortBatchLaunchMemberOutcome | null;
  evidence: CohortEvidenceMember | null;
  receiptState: string;
}

@Component({
  selector: 'app-cohort-launch-monitor',
  imports: [DecimalPipe, ReceiptLabelPipe, SectionErrorComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './cohort-launch-monitor.component.html',
  styleUrl: './cohort-launch-monitor.component.scss',
})
export class CohortLaunchMonitorComponent {
  private readonly liveRuns = inject(LiveRunsService);
  private loadEpoch = 0;

  readonly accountId = input.required<string | null>();
  readonly reloadVersion = input<number>(0);
  readonly cohort = signal<CohortBatchLaunchStatus | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  readonly rows = computed<CohortMonitorRow[]>(() => {
    const cohort = this.cohort();
    if (cohort === null) return [];
    const outcomes = new Map(cohort.outcomes.map((outcome) => [outcome.strategy_instance_id, outcome]));
    const evidence = new Map(cohort.evidence.members.map((member) => [member.strategy_instance_id, member]));
    return cohort.member_strategy_instance_ids.map((strategyInstanceId) => ({
      strategyInstanceId,
      outcome: outcomes.get(strategyInstanceId) ?? null,
      evidence: evidence.get(strategyInstanceId) ?? null,
      receiptState: outcomes.get(strategyInstanceId)?.state ?? cohort.outcomes_state,
    }));
  });

  constructor() {
    effect(() => {
      const accountId = this.accountId();
      const reloadVersion = this.reloadVersion();
      void reloadVersion;
      void this.load(accountId);
    });
  }

  async refresh(): Promise<void> {
    await this.load(this.accountId());
  }

  private async load(accountId: string | null): Promise<void> {
    const epoch = ++this.loadEpoch;
    if (accountId === null) {
      this.cohort.set(null);
      this.error.set(null);
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    try {
      const cohort = await this.liveRuns.getLatestCohortBatchLaunch(accountId);
      if (epoch !== this.loadEpoch) return;
      this.cohort.set(cohort);
    } catch (error) {
      if (epoch === this.loadEpoch) {
        this.error.set(humanError(error));
        this.cohort.set(null);
      }
    } finally {
      if (epoch === this.loadEpoch) this.loading.set(false);
    }
  }
}

function humanError(error: unknown): string {
  return error instanceof Error ? error.message : 'The durable cohort evidence is unavailable.';
}
