import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { getActionReasonCopy } from '../action-reason-codes';
import type { ActionCapability } from '../../../../api/live-instances.types';

export type DetectiveTab = 'activity' | 'diagnostics';

/**
 * Detective section — owns the Activity / Diagnostics split for the bot's
 * downstream evidence. Issue #586.
 *
 * Slot contract:
 *   <app-detective-section [activeTab]="..." (tabRequested)="...">
 *     <div slot="activity">…chart + signal + trades…</div>
 *     <div slot="diagnostics">…incidents-panel + audit table…</div>
 *   </app-detective-section>
 *
 * PRD #607 / Slice 6 (#613) — adds a right-aligned region in the tab
 * strip that renders a POISON RUN keycap only when the diagnostics tab
 * is active.  The keycap's enabled state + tooltip come from
 * ``operator_surface.actions.mark_poisoned``; the click emits
 * ``poisonRunRequested`` for the parent to route through the
 * typed-HALT confirm dialog.
 */
@Component({
  selector: 'app-detective-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './detective-section.component.html',
  styleUrl: './detective-section.component.scss',
})
export class DetectiveSectionComponent {
  readonly activeTab = input<DetectiveTab>('activity');
  readonly markPoisonedCapability = input<ActionCapability | null>(null);
  readonly requestInFlight = input<boolean>(false);

  readonly tabRequested = output<DetectiveTab>();
  readonly poisonRunRequested = output();

  onTabClick(tab: DetectiveTab): void {
    if (tab !== this.activeTab()) {
      this.tabRequested.emit(tab);
    }
  }

  readonly showPoisonKeycap = computed<boolean>(
    () => this.activeTab() === 'diagnostics',
  );

  readonly poisonKeycapDisabled = computed<boolean>(() => {
    const cap = this.markPoisonedCapability();
    if (cap === null) return true;
    return !cap.enabled || this.requestInFlight();
  });

  readonly poisonKeycapTooltip = computed<string>(() => {
    const cap = this.markPoisonedCapability();
    if (cap === null) {
      return 'Operator surface not loaded yet';
    }
    if (!cap.enabled) {
      return getActionReasonCopy(cap.disabled_reason_code);
    }
    if (this.requestInFlight()) {
      return 'Request in flight — please wait';
    }
    return 'Type HALT in the confirm dialog to mark this run poisoned';
  });

  onPoisonClick(): void {
    if (this.poisonKeycapDisabled()) return;
    this.poisonRunRequested.emit(undefined);
  }
}
