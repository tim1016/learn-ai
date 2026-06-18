import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import type { InstanceSizing, InstanceStartDefaults } from '../../../../api/live-instances.types';

/**
 * "Configuration" — merges Strategy Rules + Sizing into the v2 cockpit's
 * single configuration surface. Renders the resolved strategy + sizing
 * summary and navigates to /broker/deploy (mirroring strategy-rules-card)
 * when the operator wants to change either. Issue #585.
 *
 * Scope deferred to follow-ups (same flag-gated branch):
 *   - per-trade audit table (SizingAuditRow projection)
 *   - pinned risk-chip in the expanded header
 *   - advanced disclosure (broker address, hydration, contract sha)
 */
@Component({
  selector: 'app-configuration-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './configuration-card.component.html',
  styleUrl: './configuration-card.component.scss',
})
export class ConfigurationCardComponent {
  readonly startDefaults = input.required<InstanceStartDefaults | null>();
  readonly sizing = input.required<InstanceSizing | null>();
  readonly canRedeploy = input.required<boolean>();
  readonly redeployQueryParams = input.required<Record<string, string>>();

  readonly strategyName = computed<string | null>(
    () => this.startDefaults()?.strategy || null,
  );

  readonly sizingLabel = computed<string>(() => {
    const s = this.sizing();
    if (!s) return 'Not configured';
    if (s.preset === null) return 'Pre-policy run (legacy ledger)';
    return formatPreset(s.preset);
  });

  readonly hasConfiguration = computed<boolean>(
    () => this.strategyName() !== null,
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
