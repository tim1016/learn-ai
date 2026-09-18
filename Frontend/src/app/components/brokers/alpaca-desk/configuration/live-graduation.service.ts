import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../../api/broker.types';
import { operationUrl } from '../../../../fleet/operation-url';
import { commandBodyOf, withCommand, type ResourceTarget } from '../../../../fleet/resource-target';

export type LiveGraduationStatus = components['schemas']['LiveGraduationStatus'];
export type LiveGraduationPlan = components['schemas']['LiveGraduationPlanView'];
export type LiveGraduationOutcome = components['schemas']['LiveGraduationApplyOutcome'];

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

