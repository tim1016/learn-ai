import { Injectable, signal } from '@angular/core';
import type { HostRunnerStartRequest } from '../api/live-runs.types';

export interface ActiveBotSidebarNoticeAction {
  readonly label: string;
  readonly busyLabel: string;
  readonly runId: string;
  readonly request: HostRunnerStartRequest;
}

export interface ActiveBotSidebarNotice {
  readonly instanceId: string;
  readonly kind: 'host-runner-unreachable' | 'live-binding-invalid';
  readonly summary: string;
  readonly message: string;
  readonly command: string | null;
  readonly action: ActiveBotSidebarNoticeAction | null;
}

@Injectable({ providedIn: 'root' })
export class ActiveBotSidebarNoticeService {
  private readonly notice = signal<ActiveBotSidebarNotice | null>(null);

  readonly activeNotice = this.notice.asReadonly();

  setNotice(next: ActiveBotSidebarNotice | null): void {
    this.notice.set(next);
  }

  clearForInstance(instanceId: string | null): void {
    if (this.notice()?.instanceId === instanceId) {
      this.notice.set(null);
    }
  }
}
