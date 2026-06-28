import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';

import type {
  FleetAccountSummary,
  LiveInstanceStatus,
  OperatorNotice,
  OperatorSurfaceControlPlane,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { OperatorNoticeComponent } from '../../operator-notice/operator-notice.component';
import { ActivityTabComponent } from '../cockpit-v2/tabs/activity-tab.component';
import { AuditTabComponent } from '../cockpit-v2/tabs/audit-tab.component';
import { ConfigurationTabComponent } from '../cockpit-v2/tabs/configuration-tab.component';
import { StatusRiskTabComponent } from '../cockpit-v2/tabs/status-risk-tab.component';
import type { InnerTab } from '../cockpit-v2/lib/instance-tab-state';
import { redeployQueryParamsForStatus } from '../cockpit-v2/lib/redeploy-query-params';

const POLL_INTERVAL_MS = 4_000;

type BotControlTab = InnerTab;

interface ControlPlaneBanner {
  readonly state: OperatorSurfaceControlPlane['state'];
  readonly label: 'ATTENTION' | 'LAST-KNOWN';
  readonly demoted: boolean;
  readonly notice: string | null;
  readonly attemptText: string | null;
}

@Component({
  selector: 'app-bot-control-page',
  imports: [
    CommonModule,
    OperatorNoticeComponent,
    StatusRiskTabComponent,
    ActivityTabComponent,
    AuditTabComponent,
    ConfigurationTabComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-control-page.component.html',
  styleUrl: './bot-control-page.component.scss',
})
export class BotControlPageComponent {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  readonly instanceId = signal<string | null>(null);
  readonly status = signal<LiveInstanceStatus | null>(null);
  readonly accountSummary = signal<FleetAccountSummary | null>(null);
  readonly selectedTab = signal<BotControlTab>('status');
  readonly statusError = signal<string | null>(null);
  readonly accountSummaryError = signal<string | null>(null);
  readonly mutationError = signal<string | null>(null);
  readonly busyAction = signal<string | null>(null);

  readonly errorMessage = computed<string | null>(
    () => this.mutationError() ?? this.statusError() ?? this.accountSummaryError(),
  );

  readonly brokerEvidenceNotice = computed<OperatorNotice | null>(
    () => this.accountSummary()?.notice ?? null,
  );

  readonly hostRunnerNotice = computed(() => {
    const hostProcess = this.status()?.operator_surface.host_process ?? null;
    if (hostProcess?.state !== 'UNREACHABLE') return null;
    return {
      title: 'Host runner unreachable',
      message: hostProcess.notice ?? 'The host runner cannot be reached for this bot.',
      command: hostProcess.copyable_command,
    };
  });

  readonly controlPlaneBanner = computed<ControlPlaneBanner | null>(() => {
    const cp = this.status()?.operator_surface.control_plane ?? null;
    if (cp === null || cp.state === 'CONNECTED') return null;
    const demoted = cp.state !== 'RETRYING';
    return {
      state: cp.state,
      label: demoted ? 'LAST-KNOWN' : 'ATTENTION',
      demoted,
      notice: cp.notice,
      attemptText: cp.state === 'RETRYING' && cp.attempt > 0
        ? `retrying · attempt ${cp.attempt}`
        : null,
    };
  });

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed()).subscribe((params) => {
      const id = params.get('id');
      this.instanceId.set(id);
      this.status.set(null);
      if (id) void this.refresh(id);
    });

    const poll = setInterval(() => {
      const id = this.instanceId();
      if (id) void this.refreshStatus(id);
    }, POLL_INTERVAL_MS);
    this.destroyRef.onDestroy(() => clearInterval(poll));
  }

  selectTab(tab: BotControlTab): void {
    this.selectedTab.set(tab);
  }

  async dispatchResume(): Promise<void> {
    await this.setIntent('resume', 'Resume');
  }

  async dispatchPause(): Promise<void> {
    await this.setIntent('pause', 'Pause');
  }

  async dispatchFlattenAndPause(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set('flatten_and_pause');
    this.mutationError.set(null);
    try {
      await this.liveRuns.flattenAndPause(id, {
        action: 'pause',
        reason: 'Flatten and pause',
        updated_by: 'operator',
      });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  onGateRedeploy(): void {
    void this.router.navigate(['/broker/deploy'], { queryParams: this.redeployQueryParams() });
  }

  onGateOpenRunbook(slug: string): void {
    window.open(`/runbooks/${encodeURIComponent(slug)}`, '_blank', 'noopener');
  }

  async markPoisoned(): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    const confirmed = window.confirm(
      'Mark this run POISONED?\n\nThe bot will halt and this run will refuse future starts.',
    );
    if (!confirmed) return;
    this.busyAction.set('mark_poisoned');
    this.mutationError.set(null);
    try {
      await this.liveRuns.issueInstanceCommand(id, { verb: 'MARK_POISONED' });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  redeployQueryParams(): Record<string, string> {
    const s = this.status();
    if (!s) return {};
    return redeployQueryParamsForStatus(s);
  }

  private async refresh(id: string): Promise<void> {
    await Promise.allSettled([this.refreshStatus(id), this.refreshAccountSummary()]);
  }

  private async refreshStatus(id: string): Promise<void> {
    try {
      this.status.set(await this.liveRuns.getInstanceStatus(id));
      this.statusError.set(null);
    } catch (err) {
      this.statusError.set(this.humanError(err));
    }
  }

  private async refreshAccountSummary(): Promise<void> {
    try {
      this.accountSummary.set(await this.liveRuns.getAccountSummary());
      this.accountSummaryError.set(null);
    } catch (err) {
      this.accountSummaryError.set(this.humanError(err));
    }
  }

  private async setIntent(
    action: 'resume' | 'pause' | 'stop',
    label: string,
  ): Promise<void> {
    const id = this.instanceId();
    if (!id || this.busyAction()) return;
    this.busyAction.set(action);
    this.mutationError.set(null);
    try {
      await this.liveRuns.setInstanceDesiredState(id, {
        action,
        reason: label,
        updated_by: 'operator',
      });
      await this.refreshStatus(id);
    } catch (err) {
      this.mutationError.set(this.humanError(err));
    } finally {
      this.busyAction.set(null);
    }
  }

  private humanError(err: unknown): string {
    if (err instanceof Error && err.message) return err.message;
    return 'Could not load bot control data.';
  }
}
