import type { SizingPolicy, SizingPreset } from '../../../api/live-runs.types';

export type CustomSizingKind = 'FixedShares' | 'FixedNotional';

export const REFERENCE_PARITY_POLICY: SizingPolicy = {
  kind: 'SetHoldings',
  fraction: '1.0',
};

const FIXED_SHARES_INTEGER_RE = /^[1-9]\d*$/;
const FIXED_NOTIONAL_DECIMAL_RE = /^(?:\d+\.\d+|\d+\.?|\.\d+)$/;

export function customSizingValidationMessage(input: {
  preset: SizingPreset;
  kind: CustomSizingKind;
  rawValue: string;
}): string | null {
  if (input.preset !== 'custom') return null;
  const raw = input.rawValue.trim();
  if (raw === '') return 'Custom sizing value is required.';
  if (input.kind === 'FixedShares') {
    if (!FIXED_SHARES_INTEGER_RE.test(raw)) {
      return `FixedShares value must be a whole number >= 1 (no decimals, letters, or signs). Got "${raw}".`;
    }
    const shares = Number.parseInt(raw, 10);
    return shares < 1 ? `FixedShares value must be >= 1. Got "${raw}".` : null;
  }
  if (!FIXED_NOTIONAL_DECIMAL_RE.test(raw)) {
    return `FixedNotional value must be a positive number. Got "${raw}".`;
  }
  const notional = Number.parseFloat(raw);
  return Number.isFinite(notional) && notional > 0
    ? null
    : `FixedNotional value must be a positive number. Got "${raw}".`;
}

export function resolveDeploySizingPolicy(input: {
  sizingSurfaceIsExplicit: boolean;
  preset: SizingPreset;
  customKind: CustomSizingKind;
  customValue: string;
}): SizingPolicy {
  if (input.sizingSurfaceIsExplicit) {
    return { kind: 'StrategyExplicit' };
  }
  if (input.preset === 'reference_parity') {
    return REFERENCE_PARITY_POLICY;
  }
  if (input.preset === 'custom') {
    const raw = input.customValue.trim();
    return input.customKind === 'FixedShares'
      ? { kind: 'FixedShares', value: Number.parseInt(raw, 10) }
      : { kind: 'FixedNotional', value: raw };
  }
  return { kind: 'FixedShares', value: 1 };
}
