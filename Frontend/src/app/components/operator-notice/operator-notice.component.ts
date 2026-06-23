import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { OperatorNotice, OperatorNoticeActionKind } from '../../models/operator-notice';

const CLICKABLE_KINDS: readonly OperatorNoticeActionKind[] = [
  'open_runbook',
  'focus_cockpit_action',
  'redeploy',
];

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

  readonly tier = computed(() => this.notice().tier);

  readonly hasClickableAction = computed(() =>
    CLICKABLE_KINDS.includes(this.notice().action.kind),
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
    // Navigation/affordance is wired by the consumer in PR 1 — for now we
    // simply surface the click; routing to runbook target lands in PR 5
    // when broker-activity notices land.
  }
}
