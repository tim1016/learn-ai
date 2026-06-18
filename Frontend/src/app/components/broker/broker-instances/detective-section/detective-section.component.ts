import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { DetectiveTab } from './detective-tab';

/**
 * Detective section — tab strip owning the Activity ↔ Diagnostics switch.
 * Issue #586. The parent broker-instances component owns URL sync
 * (?tab=activity|diagnostics) so this component stays pure and easy to
 * test — it just renders the tabs and emits a request when the operator
 * clicks an inactive tab. The bodies of the two tabs are content-projected
 * by the parent (Activity = latest-signal-strip + bot-trades-table;
 * Diagnostics = can-it-trade explanations).
 */
@Component({
  selector: 'app-detective-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './detective-section.component.html',
  styleUrl: './detective-section.component.scss',
})
export class DetectiveSectionComponent {
  readonly activeTab = input.required<DetectiveTab>();

  readonly tabRequested = output<DetectiveTab>();

  onTabClick(tab: DetectiveTab): void {
    if (tab !== this.activeTab()) {
      this.tabRequested.emit(tab);
    }
  }
}
