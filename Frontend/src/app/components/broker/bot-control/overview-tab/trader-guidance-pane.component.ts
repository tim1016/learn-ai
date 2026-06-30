import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  LifecycleProjectionEventRow,
  OperatorSurface,
  TraderPrimaryRemediation,
} from '../../../../api/live-instances.types';
import {
  renderTraderRemediation,
  type RendererDispatch,
  type RenderedAction,
} from '../../cockpit-v2/lib/suggested-action-renderer';
import { TraderGuidanceTimelineComponent } from './trader-guidance-timeline.component';

@Component({
  selector: 'app-trader-guidance-pane',
  imports: [CommonModule, TraderGuidanceTimelineComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trader-guidance-pane.component.html',
  styleUrl: './trader-guidance-pane.component.scss',
})
export class TraderGuidancePaneComponent {
  readonly surface = input.required<OperatorSurface>();
  readonly timelineRows = input<LifecycleProjectionEventRow[]>([]);
  readonly timelineProjectionAvailable = input<boolean>(false);
  readonly timelineCanonicalFallbackRequired = input<boolean>(true);
  readonly timelineNotice = input<string | null>(null);
  readonly primaryRemediationSelected = output<TraderPrimaryRemediation>();

  readonly submitReadiness = computed(() => this.surface().submit_readiness);
  readonly traderGuidance = computed(() => this.surface().trader_guidance);
  readonly accountOwner = computed(() => this.surface().account_owner);
  readonly attentionGroups = computed(() => this.traderGuidance().additional_attention_groups);
  readonly advancedEvidence = computed(() => this.traderGuidance().advanced_evidence);
  readonly renderedPrimary = computed<RenderedAction | null>(() =>
    renderTraderRemediation(this.traderGuidance().primary_remediation, this.dispatch),
  );

  private readonly dispatch: RendererDispatch = {
    invokeCapability: () => this.emitCurrentRemediation(),
    focus: () => this.emitCurrentRemediation(),
    redeploy: () => this.emitCurrentRemediation(),
    openRunbook: () => this.emitCurrentRemediation(),
    invokeEndpoint: () => this.emitCurrentRemediation(),
  };

  trackEvidence(index: number, fact: { label: string; source: string | null }): string {
    return `${fact.label}:${fact.source ?? 'unknown'}:${index}`;
  }

  trackAttention(index: number, group: { code: string }): string {
    return `${group.code}:${index}`;
  }

  private emitCurrentRemediation(): void {
    const remediation = this.traderGuidance().primary_remediation;
    if (remediation.kind === 'none') return;
    this.primaryRemediationSelected.emit(remediation);
  }
}
