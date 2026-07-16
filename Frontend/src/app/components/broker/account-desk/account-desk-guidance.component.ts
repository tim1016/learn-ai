import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { Router } from '@angular/router';

import type {
  AccountDeskLens,
  OperatorBlockerAnchorKind,
} from '../../../api/operator-blocker.types';
import {
  OperatorBlockerListComponent,
  type OperatorBlockerMoveEvent,
} from '../shared/operator-blocker-list/operator-blocker-list.component';
import { AccountDeskGuidanceStore } from './account-desk-guidance-store.service';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';

/** Renders the server-provided guidance assigned to one semantic desk anchor. */
@Component({
  selector: 'app-account-desk-guidance',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OperatorBlockerListComponent],
  template: `
    <app-operator-blocker-list
      [blockers]="blockers()"
      [ariaLabel]="ariaLabel()"
      (moveSelected)="followMove($event)"
    />
  `,
})
export class AccountDeskGuidanceComponent {
  private readonly router = inject(Router);
  private readonly guidance = inject(AccountDeskGuidanceStore);
  private readonly recovery = inject(AccountDeskRecoveryStore, { optional: true });

  readonly anchor = input.required<OperatorBlockerAnchorKind>();
  readonly subjectKey = input<string | null>(null);
  readonly lens = input.required<AccountDeskLens>();
  readonly ariaLabel = input('Account guidance');
  readonly blockers = computed(() =>
    this.guidance.blockersFor(this.anchor(), this.subjectKey(), this.lens()),
  );

  followMove(event: OperatorBlockerMoveEvent): void {
    const { action } = event.move;
    if (action.kind === 'navigate') {
      void this.router.navigate([action.route], { fragment: action.fragment ?? undefined });
    } else if (action.kind === 'confirm_in_form') {
      if (this.recovery !== null) {
        this.recovery.requestDeclaredMove(event);
      } else {
        void this.router.navigate([], { fragment: action.anchor });
      }
    }
  }
}
