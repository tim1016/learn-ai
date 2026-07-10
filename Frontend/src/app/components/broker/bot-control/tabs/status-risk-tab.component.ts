// PRD #617 — Status & Risk tab.  Two-column layout: dynamic readiness
// gate list (left, scroll owner) + Current Risk cards (right).  Gates
// render the server-authored OperatorGate.suggested_action via the
// shared renderer; destructive actions reach the operator only via
// focus_action (ADR-0013 §1).

import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';

import type {
  LiveInstanceStatus,
  OperatorGate,
} from '../../../../api/live-instances.types';
import type { InnerTab } from '../lib/instance-tab-state';
import { presentSuggestedAction } from '../lib/suggested-action-renderer';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';

@Component({
  selector: 'app-status-risk-tab',
  standalone: true,
  imports: [CommonModule, AssetIdentityComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './status-risk-tab.component.html',
  styleUrl: './status-risk-tab.component.scss',
})
export class StatusRiskTabComponent {
  readonly status = input.required<LiveInstanceStatus>();

  readonly invokeResume = output();
  readonly invokePause = output();
  readonly invokeFlattenAndPause = output();
  readonly focusTab = output<InnerTab>();
  readonly redeploy = output();
  readonly openRunbook = output<string>();

  readonly nonPassingGates = computed<OperatorGate[]>(() => {
    return this.status().operator_surface.readiness_gates.filter((g) => g.status !== 'pass');
  });

  readonly passingGates = computed<OperatorGate[]>(() => {
    return this.status().operator_surface.readiness_gates.filter((g) => g.status === 'pass');
  });

  readonly currentRisk = computed(() => this.status().operator_surface.current_risk);
  readonly dailyOrderCap = computed(() => this.status().operator_surface.daily_order_cap);
  readonly sizing = computed(() => this.status().sizing);
  readonly broker = computed(() => this.status().broker);

  renderAction(gate: OperatorGate) {
    return presentSuggestedAction(gate.suggested_action);
  }

  invokeAction(gate: OperatorGate): void {
    const action = gate.suggested_action;
    if (action === null) return;
    switch (action.kind) {
      case 'invoke_capability':
        if (action.capability === 'resume') this.invokeResume.emit();
        else this.invokePause.emit();
        break;
      case 'focus_action':
        this.focusTab.emit(action.tab as InnerTab);
        break;
      case 'redeploy':
        this.redeploy.emit();
        break;
      case 'open_runbook':
        this.openRunbook.emit(action.slug);
        break;
    }
  }
}
