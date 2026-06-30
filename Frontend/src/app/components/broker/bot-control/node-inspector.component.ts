import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  LifecycleChartNode,
  LiveInstanceStatus,
  OperatorSurfaceEvidenceFact,
  OperatorSurfaceRuntimeFreshness,
} from '../../../api/live-instances.types';
import { fmtTimestampNy } from '../format';
import { bucketHelp, gateHelp, nodeHelp } from './concept-help.registry';
import { NodeReceiptsPaneComponent } from './node-receipts-pane.component';

interface RedeploySettingField {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly detail: string;
}

interface LockedEvidenceField {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly source: string;
  readonly receipt: string | null;
}

@Component({
  selector: 'app-node-inspector',
  imports: [NodeReceiptsPaneComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './node-inspector.component.html',
  styleUrl: './node-inspector.component.scss',
})
export class NodeInspectorComponent {
  readonly node = input.required<LifecycleChartNode>();
  readonly status = input.required<LiveInstanceStatus>();
  readonly hasExplicitSelection = input<boolean>(false);

  readonly redeployRequested = output();

  readonly advancedEvidence = computed<OperatorSurfaceEvidenceFact[]>(
    () => this.status().operator_surface.trader_guidance.advanced_evidence,
  );

  readonly changeForNextRunFields = computed<RedeploySettingField[]>(() => {
    const status = this.status();
    const startDefaults = status.start_defaults;
    const dailyCap = status.operator_surface.daily_order_cap;
    const sizing = status.sizing;
    const actionPlan = status.operator_surface.action_plan;
    return [
      {
        id: 'daily-order-cap',
        label: 'Daily order cap',
        value: dailyCap.limit === null ? 'Not recorded' : `${dailyCap.limit} orders per day`,
        detail: `${dailyCap.used ?? 'unknown'} used today. Change the cap through redeploy.`,
      },
      {
        id: 'sizing',
        label: 'Sizing preset',
        value: sizing?.preset ?? 'Not recorded',
        detail: `Current sizing source: ${this.sizingSourceLabel(sizing?.sizing_provenance)}.`,
      },
      {
        id: 'hydrate-policy',
        label: 'Hydrate policy',
        value: this.hydratePolicyLabel(startDefaults?.hydrate_policy),
        detail: 'Controls how the next run restores prior engine state.',
      },
      {
        id: 'action-plan',
        label: 'Action plan',
        value: actionPlan.consumption,
        detail: `Anomaly verdict: ${actionPlan.anomaly_verdict}.`,
      },
      {
        id: 'deploy-config',
        label: 'Deploy/start config',
        value: startDefaults?.strategy ?? 'Not recorded',
        detail: `Order mode: ${this.orderMode(startDefaults?.readonly)}.`,
      },
    ];
  });

  readonly lockedEvidenceFields = computed<LockedEvidenceField[]>(() => {
    const surface = this.status().operator_surface;
    return [
      {
        id: 'broker-proof',
        label: 'Broker proof',
        value: surface.broker.safety_verdict,
        source: 'operator_surface.broker.safety_verdict',
        receipt: surface.broker.connection,
      },
      {
        id: 'submit-readiness',
        label: 'Submit readiness',
        value: surface.submit_readiness.label,
        source: 'operator_surface.submit_readiness',
        receipt: surface.submit_readiness.blocking_reason_codes.join(', ') || null,
      },
      {
        id: 'reconciliation',
        label: 'Reconciliation state',
        value: surface.reconciliation?.state ?? 'NOT_AVAILABLE',
        source: 'operator_surface.reconciliation',
        receipt: surface.reconciliation?.failure_reason ?? null,
      },
      {
        id: 'account-owner',
        label: 'AccountOwner generation',
        value: surface.account_owner?.generation === null || surface.account_owner === null
          ? 'Unknown'
          : String(surface.account_owner.generation),
        source: surface.account_owner?.source ?? 'operator_surface.account_owner',
        receipt: surface.account_owner?.phase ?? null,
      },
      {
        id: 'runtime-freshness',
        label: 'Runtime freshness',
        value: this.runtimeFreshnessValue(surface.runtime_freshness),
        source: 'operator_surface.runtime_freshness',
        receipt: surface.runtime_freshness?.stale_reason_codes.join(', ') || null,
      },
    ];
  });

  readonly bucketHelp = bucketHelp;
  readonly gateHelp = gateHelp;
  readonly nodeHelp = nodeHelp;

  nodeTimestamp(node: LifecycleChartNode): string {
    return node.ts_ms_resolved ? fmtTimestampNy(node.ts_ms) : 'timestamp unresolved';
  }

  actionsForNode(nodeId: string): string {
    const labels = this.status().lifecycle_chart.actions
      .filter((action) => action.target_node_id === nodeId)
      .map((action) => action.label);
    return labels.length ? labels.join(', ') : 'None';
  }

  trackRedeployField(_: number, field: RedeploySettingField): string {
    return field.id;
  }

  trackEvidenceField(_: number, field: LockedEvidenceField): string {
    return field.id;
  }

  trackAdvancedEvidence(index: number, fact: OperatorSurfaceEvidenceFact): string {
    return `${fact.label}:${fact.source ?? 'unknown'}:${index}`;
  }

  private orderMode(readonly: boolean | null | undefined): string {
    if (readonly == null) return 'Not recorded';
    return readonly ? 'Read-only observation' : 'Order placement allowed';
  }

  private hydratePolicyLabel(policy: string | null | undefined): string {
    switch (policy) {
      case 'require':
        return 'Require previous run state';
      case 'allow_missing':
        return 'Use previous state when available';
      case 'ignore':
        return 'Start without previous state';
      case null:
      case undefined:
      case '':
        return 'Not recorded';
      default:
        return policy;
    }
  }

  private sizingSourceLabel(value: string | null | undefined): string {
    switch (value) {
      case 'live_override':
        return 'Live configuration override';
      case 'strategy_default':
        return 'Strategy default';
      case 'pre_policy':
        return 'Pre-policy run';
      case null:
      case undefined:
      case '':
        return 'not recorded';
      default:
        return value;
    }
  }

  private runtimeFreshnessValue(freshness: OperatorSurfaceRuntimeFreshness | null): string {
    if (freshness === null) return 'No live runtime evidence';
    if (freshness.posture_demoted) return 'DEMOTED';
    const domains = [
      freshness.command_loop,
      freshness.broker,
      freshness.bar_loop,
      freshness.control_plane,
    ];
    if (
      freshness.stale_reason_codes.length > 0 ||
      domains.some((domain) => domain.stale_reason_codes.length > 0)
    ) {
      return 'ATTENTION';
    }
    const nonFresh = domains.find((domain) => !['FRESH', 'NOT_APPLICABLE'].includes(domain.state));
    return nonFresh?.state ?? 'FRESH';
  }
}
