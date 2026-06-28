import { Injectable, signal } from '@angular/core';

export interface ActiveBotSidebarNotice {
  readonly instanceId: string;
  readonly message: string;
  readonly command: string | null;
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
