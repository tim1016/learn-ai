import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import type { OperatorNotice } from '../../models/operator-notice';
import { executableOperatorNoticeAction } from '../../models/operator-notice-action-contract';

@Component({
  selector: 'app-operator-notice',
  templateUrl: './operator-notice.component.html',
  styleUrl: './operator-notice.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '[attr.data-tier]': 'notice().tier',
  },
})
export class OperatorNoticeComponent {
  readonly notice = input.required<OperatorNotice>();
  readonly actionClicked = output<OperatorNotice>();

  readonly tier = computed(() => this.notice().tier);

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
