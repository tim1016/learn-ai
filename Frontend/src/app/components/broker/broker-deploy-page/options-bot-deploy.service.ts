import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../api/broker.types';
import { operationUrl } from '../../../fleet/operation-url';
import { commandBodyOf, withCommand, type ResourceTarget } from '../../../fleet/resource-target';
import type { BudgetDeployReceipt, DeploymentBudgetInput, DeploymentBudgetPreview, DeploySubmissionStatus } from '../v2-panel/lib/broker-v2-panel.service';

export type OptionsDeployRequest = components['schemas']['OptionsDeployRequest'];
export type OptionsDeploySubmission = components['schemas']['OptionsDeploySubmission'];
export type OptionsDeploymentReview = components['schemas']['OptionsDeploymentReview'];
export type OptionsBotSession = components['schemas']['OptionsBotSessionView'];
export type OptionsResumeRequest = components['schemas']['OptionsResumeRequest'];
export type OptionsBotConfiguration = components['schemas']['OptionsBotConfiguration'];

@Injectable({ providedIn: 'root' })
export class OptionsBotDeployService {
  private readonly http = inject(HttpClient);

  review(target: ResourceTarget, request: OptionsDeployRequest): Promise<OptionsDeploymentReview> {
    return firstValueFrom(this.http.post<OptionsDeploymentReview>(operationUrl('options_bot_review', target), request));
  }

  deploy(target: ResourceTarget, request: OptionsDeploySubmission): Promise<BudgetDeployReceipt> {
    if (target.idempotencyKey !== request.submission_key) throw new Error('The deployment key must match its frozen target.');
    return firstValueFrom(this.http.post<BudgetDeployReceipt>(
      operationUrl('options_bot_deploy', target),
      commandBodyOf(withCommand(target, 'bot_action', request.submission_key), request),
    ));
  }

  submission(target: ResourceTarget, submissionKey: string): Promise<DeploySubmissionStatus> {
    return firstValueFrom(this.http.get<DeploySubmissionStatus>(operationUrl('options_bot_deploy_submission_read', {
      ...target, submissionKey,
    })));
  }

  session(target: ResourceTarget, sid: string): Promise<OptionsBotSession> {
    return firstValueFrom(this.http.get<OptionsBotSession>(operationUrl('options_bot_session_read', { ...target, sid })));
  }

  reviewResume(target: ResourceTarget, sid: string, budget: DeploymentBudgetInput | null): Promise<DeploymentBudgetPreview> {
    return firstValueFrom(this.http.post<DeploymentBudgetPreview>(operationUrl('options_bot_resume_review', { ...target, sid }), budget));
  }

  resume(target: ResourceTarget, sid: string, request: OptionsResumeRequest): Promise<components['schemas']['BotStatusView']> {
    if (target.idempotencyKey !== request.idempotency_key) throw new Error('The resume key must match its frozen target.');
    return firstValueFrom(this.http.post<components['schemas']['BotStatusView']>(
      operationUrl('options_bot_resume', { ...target, sid }),
      commandBodyOf(withCommand(target, 'bot_action', request.idempotency_key), request),
    ));
  }
}
