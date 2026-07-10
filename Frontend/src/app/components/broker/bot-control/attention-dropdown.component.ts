import { ChangeDetectionStrategy, Component, computed, input, model, output } from '@angular/core';

import type {
  OperatorSurfaceAttentionGroup,
  OperatorSurfaceTraderGuidance,
  TraderPrimaryRemediation,
} from '../../../api/live-instances.types';
import {
  presentTraderRemediation,
  type PresentedAction,
} from './lib/suggested-action-renderer';

interface AttentionActionRow {
  readonly group: OperatorSurfaceAttentionGroup;
  readonly action: PresentedAction | null;
}

@Component({
  selector: 'app-attention-dropdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './attention-dropdown.component.html',
  styleUrl: './attention-dropdown.component.scss',
})
export class AttentionDropdownComponent {
  readonly guidance = input.required<OperatorSurfaceTraderGuidance>();
  readonly groups = input.required<OperatorSurfaceAttentionGroup[]>();
  readonly open = model<boolean>(false);
  readonly remediationSelected = output<TraderPrimaryRemediation>();

  readonly actionRows = computed<AttentionActionRow[]>(() =>
    this.groups().map((group) => ({
      group,
      action: presentTraderRemediation(group.remediation),
    })),
  );

  toggle(): void {
    this.open.update((open) => !open);
  }

  close(): void {
    this.open.set(false);
  }

  trackRow(_: number, row: AttentionActionRow): string {
    return row.group.code;
  }

  invoke(row: AttentionActionRow): void {
    if (row.group.remediation.kind === 'none') return;
    this.remediationSelected.emit(row.group.remediation);
  }
}
