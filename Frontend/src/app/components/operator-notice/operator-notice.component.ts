import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import {
  executableOperatorNoticeAction,
  type RenderableNotice,
} from '../../models/operator-notice-action-contract';

const ACTIONABILITY_LABEL: Record<RenderableNotice['actionability'], string> = {
  actuatable: 'Action available',
  routed: 'Check elsewhere',
  self_resolving: 'Clears automatically',
  no_remedy: 'No remedy',
};

@Component({
  selector: 'app-operator-notice',
  templateUrl: './operator-notice.component.html',
  styleUrl: './operator-notice.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '[attr.data-tier]': 'notice().tier',
  },
})
export class OperatorNoticeComponent<T extends RenderableNotice> {
  readonly notice = input.required<T>();
  readonly actionClicked = output<T>();

  readonly tier = computed(() => this.notice().tier);
  readonly actionabilityLabel = computed(() => ACTIONABILITY_LABEL[this.notice().actionability]);

  readonly hasClickableAction = computed(() =>
    executableOperatorNoticeAction(this.notice()) !== null,
  );

  readonly hasInertActionLabel = computed(() => {
    const action = this.notice().action;
    return action.kind === 'external_manual_check' && !!action.label;
  });

  readonly forensicFactEntries = computed(() =>
    Object.entries(this.notice().forensic_facts ?? {}).map(([key, value]) => ({
      key,
      value: value === null ? 'null' : String(value),
    })),
  );

  onActionClick(): void {
    this.actionClicked.emit(this.notice());
  }
}
