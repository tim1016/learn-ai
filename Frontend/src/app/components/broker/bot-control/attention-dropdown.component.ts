import { ChangeDetectionStrategy, Component, computed, input, model, output } from '@angular/core';

import type {
  OperatorSurfaceAttentionGroup,
  OperatorSurfaceTraderGuidance,
  TraderPrimaryRemediation,
} from '../../../api/live-instances.types';
import {
  renderTraderRemediation,
  type RenderedAction,
  type RendererDispatch,
} from './lib/suggested-action-renderer';

interface AttentionActionRow {
  readonly group: OperatorSurfaceAttentionGroup;
  readonly action: RenderedAction | null;
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
      action: renderTraderRemediation(group.remediation, this.dispatch),
    })),
  );

  private readonly dispatch: RendererDispatch = {
    invokeCapability: () => this.emitCurrentRemediation(),
    focus: () => this.emitCurrentRemediation(),
    redeploy: () => this.emitCurrentRemediation(),
    openRunbook: () => this.emitCurrentRemediation(),
    invokeEndpoint: () => this.emitCurrentRemediation(),
  };

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

  private emitCurrentRemediation(): void {
    // Row actions call invoke(row), which has the concrete group. This dispatch
    // only satisfies the shared renderer contract and should not fire directly.
  }
}
