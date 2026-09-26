import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../../api/broker.types';
import { operationUrl } from '../../../../fleet/operation-url';
import { commandBodyOf, withCommand, withEntity, type ResourceTarget } from '../../../../fleet/resource-target';

export type ArmingPlan = components['schemas']['ArmingPlanView'];
export type ArmingStatus = components['schemas']['ArmingStatusView'];

@Injectable({ providedIn: 'root' })
export class LiveArmingService {
  private readonly http = inject(HttpClient);

  status(target: ResourceTarget, sid: string): Promise<ArmingStatus> {
    return firstValueFrom(this.http.get<ArmingStatus>(operationUrl('live_arming_status', { ...target, sid })));
  }

  prepare(target: ResourceTarget, sid: string): Promise<ArmingPlan> {
    const command = withCommand(withEntity(target, sid), 'custody_command', crypto.randomUUID());
    return firstValueFrom(this.http.post<ArmingPlan>(operationUrl('live_arming_plan', { ...command, sid }),
      commandBodyOf(command, {})));
  }

  apply(target: ResourceTarget, sid: string, plan: ArmingPlan, token: string): Promise<ArmingStatus> {
    const command = withCommand(withEntity(target, sid), 'custody_command', `arming:${plan.plan_id}`);
    return firstValueFrom(this.http.post<ArmingStatus>(operationUrl('live_arming_apply', { ...command, sid }),
      commandBodyOf(command, { plan_id: plan.plan_id, confirmation_token: token })));
  }

  disarm(target: ResourceTarget, sid: string): Promise<ArmingStatus> {
    const command = withCommand(withEntity(target, sid), 'custody_command', crypto.randomUUID());
    return firstValueFrom(this.http.post<ArmingStatus>(operationUrl('live_arming_disarm', { ...command, sid }),
      commandBodyOf(command, {})));
  }
}
