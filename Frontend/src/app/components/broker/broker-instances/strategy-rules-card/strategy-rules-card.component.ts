import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';
import type {
  InstanceProvenance,
  InstanceSizing,
  InstanceStartDefaults,
} from '../../../../api/live-instances.types';

interface PrimaryRow {
  label: string;
  value: string;
}

interface AdvancedRow {
  label: string;
  value: string;
  mono?: boolean;
}

/**
 * "Strategy Rules" — one card that answers *"what is this bot allowed to do?"*.
 *
 * Issue #565 PR 7 — merges strategy / order-mode / daily-cap into a single
 * decision-priority surface and exposes the existing deploy identity (broker
 * address, hydration mode, submission mode, strategy contract) behind a
 * `[Show advanced ▾]` disclosure so the trader default view stays calm.
 *
 * Reads only fields the engine already emits via the status contract
 * (`start_defaults` + `provenance` + `sizing.preset`). No new backend math.
 */
@Component({
  selector: 'app-strategy-rules-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './strategy-rules-card.component.html',
  styleUrl: './strategy-rules-card.component.scss',
})
export class StrategyRulesCardComponent {
  readonly startDefaults = input.required<InstanceStartDefaults | null>();
  readonly provenance = input.required<InstanceProvenance | null>();
  readonly sizing = input.required<InstanceSizing | null>();
  readonly instanceId = input.required<string>();
  readonly canRedeploy = input.required<boolean>();
  readonly redeployQueryParams = input.required<Record<string, string>>();

  /** Emitted when the operator clicks `[Redeploy with new rules]`. The parent
   * decides whether to show a confirmation (User Story #21: warn that a live
   * bot continues unchanged) before navigating to /broker/deploy. */
  readonly redeployRequested = output();

  readonly primaryRows = computed<PrimaryRow[]>(() => {
    const d = this.startDefaults();
    const s = this.sizing();
    const rows: PrimaryRow[] = [];
    rows.push({ label: 'Strategy', value: d?.strategy || '(unknown)' });
    rows.push({ label: 'Order mode', value: this.orderModeLabel(d?.readonly) });
    rows.push({
      label: 'Daily cap',
      value:
        d?.max_orders_per_day != null
          ? `${d.max_orders_per_day} orders / day`
          : '(not recorded)',
    });
    rows.push({ label: 'Sizing', value: this.sizingLabel(s) });
    return rows;
  });

  readonly advancedRows = computed<AdvancedRow[]>(() => {
    const d = this.startDefaults();
    const p = this.provenance();
    const rows: AdvancedRow[] = [];
    if (d?.ibkr_host) {
      rows.push({ label: 'Broker address', value: d.ibkr_host, mono: true });
    }
    rows.push({
      label: 'Hydration mode',
      value: d?.hydrate_policy ?? '(not recorded)',
    });
    rows.push({
      label: 'Submission mode',
      value: this.orderModeLabel(d?.readonly),
    });
    if (p?.strategy_spec_path) {
      rows.push({
        label: 'Strategy contract',
        value: p.strategy_spec_path,
        mono: true,
      });
    }
    if (p?.strategy_spec_sha256) {
      rows.push({
        label: 'Contract SHA',
        value: shortSha(p.strategy_spec_sha256),
        mono: true,
      });
    }
    if (p?.qc_cloud_backtest_id) {
      rows.push({
        label: 'QC Cloud backtest',
        value: p.qc_cloud_backtest_id,
        mono: true,
      });
    }
    return rows;
  });

  onRedeployClick(): void {
    this.redeployRequested.emit(undefined);
  }

  private orderModeLabel(readonly: boolean | undefined): string {
    if (readonly === true) return 'Read-only (no order submission)';
    if (readonly === false) return 'Live submission';
    return '(not recorded)';
  }

  private sizingLabel(s: InstanceSizing | null): string {
    if (!s) return '(not recorded)';
    if (s.preset === null) return 'Pre-policy run (legacy ledger)';
    return formatPreset(s.preset);
  }
}

function shortSha(sha: string): string {
  return sha ? sha.slice(0, 12) : '';
}

function formatPreset(preset: string): string {
  if (preset === 'explicit') return 'Explicit (strategy-defined)';
  return preset
    .split('_')
    .map((w) => (w.length ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}
