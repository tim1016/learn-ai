import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { InstanceSizing } from '../../../api/live-instances.types';

interface SizingFact {
  label: string;
  value: string;
  /** Pill class for terse stamp values (governed_by, sizing_provenance). */
  pill?: 'live-config' | 'strategy-explicit' | 'live-override' | 'reference-native' | null;
}

/**
 * ADR 0009 — Sizing card.
 *
 * Renders the instance's position-sizing decision and its provenance stamps.
 * PR1 ships the **static facts** section; PR4 adds live derivation, PR6 adds
 * the per-trade audit list.
 *
 * Three render states:
 *  1. Sizing-aware run — preset, kind/value, governed_by, sizing_provenance
 *  2. Legacy / pre-policy run (policy === null) — honest "Pre-policy run" badge
 *  3. Nothing deployed (`sizing` input is null) — the card doesn't render at all
 *     (controlled by the parent component).
 */
@Component({
  selector: 'app-broker-sizing-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-sizing-card.component.html',
  styleUrl: './broker-sizing-card.component.scss',
})
export class BrokerSizingCardComponent {
  readonly sizing = input.required<InstanceSizing>();

  readonly isLegacy = computed<boolean>(() => this.sizing().policy === null);

  readonly presetLabel = computed<string>(() => {
    switch (this.sizing().preset) {
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
  });

  readonly policyDescription = computed<string>(() => {
    const policy = this.sizing().policy;
    if (policy === null) return 'Pre-policy run';
    switch (policy.kind) {
      case 'FixedShares':
        return `${policy.value} share${policy.value === 1 ? '' : 's'} per signal`;
      case 'SetHoldings':
        return `Target ${policy.fraction} of portfolio value`;
      case 'FixedNotional':
        return `Target $${policy.value} notional`;
      case 'StrategyExplicit':
        return 'Strategy supplies its own quantity';
    }
  });

  readonly facts = computed<SizingFact[]>(() => {
    const s = this.sizing();
    if (s.policy === null) return [];
    const rows: SizingFact[] = [
      { label: 'Preset', value: this.presetLabel(), pill: null },
      { label: 'Sized', value: this.policyDescription(), pill: null },
      {
        label: 'Governed by',
        value: s.governed_by === 'live_config' ? 'Deploy-form policy' : 'Strategy code',
        pill: s.governed_by === 'live_config' ? 'live-config' : 'strategy-explicit',
      },
      {
        label: 'Provenance',
        value:
          s.sizing_provenance === 'reference_native'
            ? 'Matches QC audit copy'
            : s.sizing_provenance === 'spec_default'
              ? 'Spec default'
              : 'Live override',
        pill: s.sizing_provenance === 'reference_native' ? 'reference-native' : 'live-override',
      },
    ];
    return rows;
  });
}
