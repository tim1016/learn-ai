import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';
import { ButtonModule } from 'primeng/button';

import { JobsService } from '../../services/jobs.service';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { RecordControlsComponent } from '../../shared/research-record/record-controls.component';
import { RecordPoller } from '../../shared/research-record/record-poller';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { GridSearchResultComponent } from '../grid-search/grid-search-result.component';
import { isTerminal } from '../grid-search/grid-search.types';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { WalkForwardStudyService } from './walk-forward-study.service';
import type { Fold, WalkForwardStudyDetail } from './walk-forward-study.types';

export interface OpenSweep {
  readonly id: string;
  readonly fold: Fold;
  readonly phase: 'train' | 'test';
}

/**
 * One study's result (PRD #1925 "Frontend"): the verdict with its coverage,
 * the fold table (winner, training and test Sharpe, retention, trades), the
 * per-fold sweeps opened in place through Grid Search's result view, and
 * Finish, Cancel and Delete. Polls while the study runs.
 */
@Component({
  selector: 'app-walk-forward-study-result',
  imports: [AssetIdentityComponent, ButtonModule, DecimalPipe, PercentPipe, GridSearchResultComponent, RecordControlsComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './walk-forward-study-result.component.html',
  styleUrl: './walk-forward-study-result.component.scss',
})
export class WalkForwardStudyResultComponent {
  private readonly service = inject(WalkForwardStudyService);
  private readonly jobs = inject(JobsService);
  private readonly destroyRef = inject(DestroyRef);

  readonly studyId = input.required<string>();
  readonly strategies = input<readonly StrategyInfo[]>([]);
  /** Poll interval while the study runs; tests set 0 to disable. */
  readonly pollMs = input(3000);
  readonly deleted = output<string>();
  readonly closed = output();

  readonly detail = signal<WalkForwardStudyDetail | null>(null);
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);
  readonly openSweep = signal<OpenSweep | null>(null);

  protected readonly strategyName = computed(() => {
    const key = this.detail()?.strategy_key;
    return key ? (this.strategies().find((s) => s.name === key)?.display_name ?? null) : null;
  });
  protected readonly running = computed(() => {
    const status = this.detail()?.status;
    return status !== undefined && !isTerminal(status);
  });

  private readonly poller = new RecordPoller(this.destroyRef);
  /** Set by Finish: keep polling until the row shows the new attempt running (the worker's claim races the 202). */
  private awaitingAttempt = false;

  constructor() {
    effect(() => {
      const id = this.studyId();
      untracked(() => void this.reload(id));
    });
  }

  /** Revision of the latest reload; a poll that resolves late must not restore stale state. */
  private revision = 0;

  async reload(id: string = this.studyId()): Promise<void> {
    const revision = ++this.revision;
    try {
      const detail = await this.service.get(id);
      if (revision !== this.revision) return;
      this.detail.set(detail);
      this.error.set(null);
      if (!isTerminal(detail.status)) this.awaitingAttempt = false;
      this.schedulePoll();
    } catch {
      if (revision === this.revision) this.error.set('This study could not be loaded.');
    }
  }

  winnerText(fold: Fold): string {
    return fold.winner_params ? Object.entries(fold.winner_params).map(([k, v]) => `${k}=${v}`).join(', ') : '—';
  }

  open(fold: Fold, phase: 'train' | 'test'): void {
    const id = phase === 'train' ? fold.train_search_id : fold.test_search_id;
    if (id) this.openSweep.set({ id, fold, phase });
  }

  closeSweep(): void {
    this.openSweep.set(null);
  }

  async finish(): Promise<void> {
    const detail = this.detail();
    if (!detail?.resumable) return;
    this.busy.set(true);
    try {
      await this.service.finish(detail);
      this.awaitingAttempt = true;
      await this.reload();
    } catch {
      this.error.set('Finish was not accepted. Reload and check the refusal reason.');
    } finally {
      this.busy.set(false);
    }
  }

  async cancel(): Promise<void> {
    const jobId = this.detail()?.job_id;
    if (!jobId || !this.running()) return;
    this.busy.set(true);
    try {
      await this.jobs.cancelJob(jobId);
      await this.reload();
    } catch {
      this.error.set('Cancellation could not be requested. Try again.');
    } finally {
      this.busy.set(false);
    }
  }

  async delete(): Promise<void> {
    const id = this.studyId();
    this.busy.set(true);
    try {
      await this.service.delete(id);
      this.poller.stop();
      this.deleted.emit(id);
    } catch {
      this.error.set('The study could not be deleted. If it is running, cancellation is being acknowledged; try again shortly.');
    } finally {
      this.busy.set(false);
    }
  }

  private schedulePoll(): void {
    if (this.running() || this.awaitingAttempt) this.poller.schedule(this.pollMs(), () => void this.reload());
    else this.poller.stop();
  }
}
