import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { operationUrl } from '../../../../fleet/operation-url';
import { commandBodyOf, withCommand, type ResourceTarget } from '../../../../fleet/resource-target';

export interface LiveGraduationStatus {
  readonly account_id: string;
  readonly configured_mode: 'live';
  readonly authority: 'shadow' | 'live' | 'unavailable';
  readonly state: 'review_available' | 'graduated' | 'blocked';
  readonly headline: string;
  readonly detail: string;
  readonly next_action: string | null;
  readonly restart_managed: boolean;
}

export interface LiveGraduationPlan {
  readonly plan_id: string;
  readonly confirmation_token: string;
  readonly account_id: string;
  readonly created_at_ms: number;
  readonly expires_at_ms: number;
  readonly broker_observed_at_ms: number;
  readonly position_count: number;
  readonly open_order_count: number;
  readonly stopped_bot_ids: readonly string[];
  readonly backup_reference: string;
  readonly daily_loss_fraction: number;
  readonly daily_loss_usd: number;
  readonly arming_max_sessions: number;
  readonly extended_hours_entry_bps: number;
  readonly extended_hours_exit_bps: number;
  readonly consequence: string;
}

export interface LiveGraduationOutcome {
  readonly account_id: string;
  readonly plan_id: string;
  readonly state: 'restart_scheduled';
  readonly receipt_reference: string;
  readonly activated_at_ms: number;
  readonly message: string;
}

@Injectable({ providedIn: 'root' })
export class LiveGraduationService {
  private readonly http = inject(HttpClient);

  readStatus(clerkId: string, accountId: string): Promise<LiveGraduationStatus> {
    return firstValueFrom(
      this.http.get<LiveGraduationStatus>(
        operationUrl('live_graduation_status', {
          broker: 'alpaca',
          clerkId,
          accountId,
        }),
      ),
    );
  }

  prepare(target: ResourceTarget): Promise<LiveGraduationPlan> {
    const command = withCommand(target, 'custody_command', target.idempotencyKey);
    return firstValueFrom(
      this.http.post<LiveGraduationPlan>(
        operationUrl('live_graduation_plan', command),
        commandBodyOf(command, {}),
      ),
    );
  }

  apply(target: ResourceTarget, plan: LiveGraduationPlan): Promise<LiveGraduationOutcome> {
    const command = withCommand(target, 'custody_command', target.idempotencyKey);
    return firstValueFrom(
      this.http.post<LiveGraduationOutcome>(
        operationUrl('live_graduation_apply', command),
        commandBodyOf(command, {
          plan_id: plan.plan_id,
          confirmation_token: plan.confirmation_token,
        }),
      ),
    );
  }
}

