import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { WorkspaceStateBlockComponent } from '../workspace/workspace-state-block.component';
import type { LakeHoldingControl } from './create-lake-holding';
import {
  CANCEL_CAPTURE_ACTION_ID,
  CAPTURE_ACTION_ID,
  DISMISS_CAPTURE_ACTION_ID,
  describeLakeHolding,
} from './lake-holding';

/**
 * What a lake study's page says before Run about the sessions the lake does not hold yet, and the action that captures
 * them (#3521). Pure presentation of a page's `createLakeHolding` control: it warns, and never blocks Run or starts a
 * capture by itself. It draws nothing while a count is being read, or when the lake holds every session the run needs.
 */
@Component({
  selector: 'app-lake-holding-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [WorkspaceStateBlockComponent],
  template: `
    @if (state(); as content) {
      <app-workspace-state-block [state]="content" (actionTriggered)="onAction($event)" />
    }
  `,
})
export class LakeHoldingNoticeComponent {
  readonly holding = input.required<LakeHoldingControl>();

  protected readonly state = computed(() => {
    const holding = this.holding();
    return describeLakeHolding(holding.reading(), holding.capture(), holding.progress());
  });

  protected onAction(id: string): void {
    const holding = this.holding();
    if (id === CAPTURE_ACTION_ID) void holding.startCapture();
    else if (id === CANCEL_CAPTURE_ACTION_ID) void holding.cancelCapture();
    else if (id === DISMISS_CAPTURE_ACTION_ID) holding.dismissCapture();
  }
}
