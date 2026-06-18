import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

export type DetectiveTab = 'activity' | 'diagnostics';

/**
 * Detective section — owns the Activity / Diagnostics split for the bot's
 * downstream evidence. Always rendered; the `tabbed` input gates whether
 * the two slots collapse into a single tab strip (cockpit-v2 mode) or
 * render inline as a flat list (legacy mode). Issue #586.
 *
 * Slot contract:
 *   <app-detective-section [tabbed]="..." [activeTab]="...">
 *     <div slot="activity">…chart + signal + trades…</div>
 *     <div slot="diagnostics">…incidents-panel…</div>
 *   </app-detective-section>
 */
@Component({
  selector: 'app-detective-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './detective-section.component.html',
  styleUrl: './detective-section.component.scss',
})
export class DetectiveSectionComponent {
  readonly tabbed = input.required<boolean>();
  readonly activeTab = input<DetectiveTab>('activity');

  readonly tabRequested = output<DetectiveTab>();

  onTabClick(tab: DetectiveTab): void {
    if (tab !== this.activeTab()) {
      this.tabRequested.emit(tab);
    }
  }
}
