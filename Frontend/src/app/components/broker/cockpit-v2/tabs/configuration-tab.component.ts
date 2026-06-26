// PRD #617 — Configuration tab.  Server-authored deployment reference
// data; Redeploy → existing route with prefill.  Redeploy copy is
// server-honest ("creates a new run identity. Does not start the
// host-owned process").  Pre-trade checklist is NOT mirrored here.

import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import { fmtTimestampNy } from '../../format';

@Component({
  selector: 'app-configuration-tab',
  standalone: true,
  imports: [CommonModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './configuration-tab.component.html',
  styleUrl: './configuration-tab.component.scss',
})
export class ConfigurationTabComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly redeployQueryParams = input<Record<string, string>>({});

  readonly startDefaults = computed(() => this.status().start_defaults);
  readonly sizing = computed(() => this.status().sizing);
  readonly lineage = computed(() => this.status().lineage);
  readonly provenance = computed(() => this.status().provenance);
  readonly configuration = computed(() => this.status().operator_surface.configuration);
  readonly dailyOrderCap = computed(() => this.status().operator_surface.daily_order_cap);
  readonly actionPlan = computed(() => this.status().action_plan);
  readonly actionPlanProj = computed(() => this.status().operator_surface.action_plan);
  readonly fmtTimestampNy = fmtTimestampNy;

  orderMode(readonly: boolean | null | undefined): string {
    return readonly ? 'Read-only observation' : 'Order placement allowed';
  }

  hydratePolicyLabel(policy: string | null | undefined): string {
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
        return '—';
      default:
        return policy;
    }
  }

  instrumentSurfaceLabel(surface: string | null | undefined): string {
    switch (surface) {
      case 'explicit':
        return 'Explicit instrument list';
      case 'inferred':
        return 'Inferred from strategy';
      case null:
      case undefined:
      case '':
        return '—';
      default:
        return surface;
    }
  }

  sizingSourceLabel(value: string | null | undefined): string {
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
        return '—';
      default:
        return value;
    }
  }

  governedByLabel(value: string | null | undefined): string {
    switch (value) {
      case 'live_config':
        return 'Live configuration';
      case 'strategy_spec':
        return 'Strategy specification';
      case null:
      case undefined:
      case '':
        return '—';
      default:
        return value;
    }
  }
}
