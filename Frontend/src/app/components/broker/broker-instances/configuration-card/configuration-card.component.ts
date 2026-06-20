import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import type {
  InstanceBrokerView,
  InstanceProvenance,
  InstanceSizing,
  InstanceStartDefaults,
  OperatorSurfaceConfiguration,
  OperatorSurfaceCurrentRisk,
  OperatorSurfaceDailyOrderCap,
} from '../../../../api/live-instances.types';

/**
 * "Configuration" card — the cockpit's single configuration surface
 * (PRD #607 / Slice 4 / #611).
 *
 * Body rows when expanded:
 *   - STRATEGY KEY (start_defaults.strategy + [vN spec] badge)
 *   - DAILY CAP {limit} orders/day [used {used} today] — values come
 *     directly from operator_surface.daily_order_cap.{used, limit}
 *     (the engine readiness sidecar's structured fields); the gate
 *     prose is no longer parsed (#608).
 *   - SIZING SUMMARY {policy} + {preset} + provenance badge derived
 *     from sizing.sizing_provenance (reference_native / live_override /
 *     spec_default / unknown).  The previously planned ``[SHA verified]``
 *     label is removed — the underlying enum has no SHA-verification
 *     semantics.
 *
 * Pinned risk-chip in the expanded header sources from
 * operator_surface.current_risk + the unrealized_pnl field on the broker
 * view.  ``null`` posture / pending render ``—``; ``null``
 * unrealized_pnl is omitted entirely (no fake ``0.00``).
 *
 * Server-driven collapse: ``data-collapsed`` reflects
 * ``operator_surface.configuration.verdict``.  On attention verdicts
 * the toggle is absent from the DOM (Option A semantics; see Slice 2).
 *
 * ORDER MODE row, ▸ ADVANCED disclosure, and ▸ SIZING DETAIL
 * disclosure are NOT rendered — they live on the deploy form,
 * reachable via REDEPLOY.
 */
@Component({
  selector: 'app-configuration-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DecimalPipe],
  templateUrl: './configuration-card.component.html',
  styleUrl: './configuration-card.component.scss',
  host: {
    '[attr.data-verdict]': 'verdictAttr()',
    '[attr.data-collapsed]': 'collapsedAttr()',
  },
})
export class ConfigurationCardComponent {
  readonly startDefaults = input.required<InstanceStartDefaults | null>();
  readonly sizing = input.required<InstanceSizing | null>();
  readonly provenance = input.required<InstanceProvenance | null>();
  readonly broker = input.required<InstanceBrokerView | null>();
  readonly configuration = input.required<OperatorSurfaceConfiguration>();
  readonly currentRisk = input.required<OperatorSurfaceCurrentRisk>();
  readonly dailyOrderCap = input.required<OperatorSurfaceDailyOrderCap>();
  readonly canRedeploy = input.required<boolean>();
  readonly redeployQueryParams = input.required<Record<string, string>>();

  readonly strategyName = computed<string | null>(
    () => this.startDefaults()?.strategy || null,
  );

  readonly specSchemaBadge = computed<string | null>(() => {
    const v = this.provenance()?.schema_version;
    return v == null ? null : `v${v} spec`;
  });

  readonly sizingPresetLabel = computed<string>(() => {
    const s = this.sizing();
    if (!s) return 'Not configured';
    if (s.preset === null) return 'Pre-policy run (legacy ledger)';
    return formatPreset(s.preset);
  });

  readonly sizingProvenanceBadge = computed<string>(() => {
    const p = this.sizing()?.sizing_provenance ?? null;
    switch (p) {
      case 'reference_native':
        return 'reference native';
      case 'live_override':
        return 'live override';
      case 'spec_default':
        return 'spec default';
      default:
        return 'provenance unknown';
    }
  });

  readonly hasConfiguration = computed<boolean>(() => this.strategyName() !== null);

  // ─ Daily cap (#611 §"DAILY CAP" — read structured fields) ────────
  readonly dailyCapUsed = computed<number | null>(() => this.dailyOrderCap().used);
  readonly dailyCapLimit = computed<number | null>(() => this.dailyOrderCap().limit);

  // ─ Pinned risk-chip ──────────────────────────────────────────────
  readonly riskChipPosture = computed<string>(() => this.currentRisk().posture);
  readonly riskChipPending = computed<number | null>(
    () => this.currentRisk().pending_order_count,
  );
  readonly riskChipUnrealizedPnl = computed<number | null>(
    () => this.currentRisk().unrealized_pnl,
  );

  // ─ Server-driven collapse + verdict-glow ─────────────────────────
  readonly verdictAttr = computed<'ready' | 'degraded' | 'blocked' | 'unknown'>(() => {
    switch (this.configuration().verdict) {
      case 'READY':
        return 'ready';
      case 'ATTENTION':
        return 'degraded';
      case 'UNKNOWN':
      default:
        return 'unknown';
    }
  });

  readonly isAttentionCard = computed<boolean>(
    () => this.configuration().verdict !== 'READY',
  );

  readonly collapsedAttr = computed<'true' | 'false'>(() =>
    this.configuration().verdict === 'READY' ? 'true' : 'false',
  );
}

function formatPreset(preset: string): string {
  switch (preset) {
    case 'safe_canary':
      return 'Safe canary';
    case 'reference_parity':
      return 'Reference parity';
    case 'custom':
      return 'Custom';
    case 'explicit':
      return 'Self-sized (strategy explicit)';
    default:
      return '—';
  }
}
