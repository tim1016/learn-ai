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

  readonly headline = computed<OperatorNotice | null>(
    () => this.freshness()?.headline ?? null,
  );

  readonly additionalReasons = computed<OperatorNotice[]>(
    () => this.freshness()?.additional_reasons ?? [],
  );
}
