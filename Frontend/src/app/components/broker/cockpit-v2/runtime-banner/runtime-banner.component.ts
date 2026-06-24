import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type {
  OperatorNotice,
  OperatorSurfaceRuntimeFreshness,
} from '../../../../api/live-instances.types';
import { OperatorNoticeComponent } from '../../../operator-notice/operator-notice.component';

@Component({
  selector: 'app-runtime-banner',
  templateUrl: './runtime-banner.component.html',
  styleUrl: './runtime-banner.component.scss',
  imports: [OperatorNoticeComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RuntimeBannerComponent {
  readonly freshness = input.required<OperatorSurfaceRuntimeFreshness | null>();
  /** PR 2 / PR 5 — post-halt watchdog incident headline. When non-null, rendered
   *  above the freshness headline. Critical-tier incidents are always shown above
   *  lower-priority notices per ADR-0013 §3. */
  readonly incidentHeadline = input<OperatorNotice | null>(null);

  readonly headline = computed<OperatorNotice | null>(
    () => this.freshness()?.headline ?? null,
  );

  readonly additionalReasons = computed<OperatorNotice[]>(
    () => this.freshness()?.additional_reasons ?? [],
  );

  /** True when either the incident headline or the freshness headline is visible. */
  readonly hasBannerContent = computed<boolean>(
    () => this.incidentHeadline() !== null || this.headline() !== null,
  );
}
