import type { SearchValue } from './options-golden-search.types';

export type RuleKey = 'stop_loss' | 'momentum' | 'reentry_on_stop' | 'no_reentry_after';
export interface RuleDraft {
  key: RuleKey;
  vary: boolean;
  kind: string;
  minimum: string;
  maximum: string;
  step: string;
  off: boolean;
  priority: number;
}
export const RULE_LABELS: Record<RuleKey, string> = { stop_loss: 'Stop loss', momentum: 'Entry momentum', reentry_on_stop: 'Re-entry after stop', no_reentry_after: 'Re-entry cutoff' };
export const RULE_KINDS: Record<RuleKey, readonly { value: string; label: string }[]> = {
  stop_loss: [{ value: 'percent', label: 'Percent of option premium (%)' }, { value: 'points', label: 'Option premium points' }],
  momentum: [{ value: 'percent_up', label: 'Premium rise (%)' }, { value: 'percent_down', label: 'Premium fall (%)' }, { value: 'points_up', label: 'Premium rise (points)' }, { value: 'points_down', label: 'Premium fall (points)' }],
  reentry_on_stop: [{ value: 'at_once', label: 'Immediately after stop' }, { value: 'on_momentum', label: 'After entry momentum triggers again' }],
  no_reentry_after: [{ value: 'close', label: 'Minutes before session close' }, { value: 'open', label: 'Minutes after session open' }],
};

export function newRule(key: RuleKey, saved: SearchValue): RuleDraft {
  const value = saved !== null && typeof saved === 'object' ? saved : {};
  const amount = String(key === 'reentry_on_stop' ? value['count'] ?? 1 : key === 'no_reentry_after' ? Number(value['offset_ms'] ?? 60000) / 60000 : value['value'] ?? 10);
  return { key, vary: false, kind: String(value['kind'] ?? value['anchor'] ?? RULE_KINDS[key][0].value), minimum: amount, maximum: amount, step: key === 'reentry_on_stop' || key === 'no_reentry_after' ? '1' : '5', off: false, priority: 5 };
}

/** Exact form-value expansion on the option rule schema's four-decimal lattice. */
export function ruleValues(draft: RuleDraft): readonly SearchValue[] | null {
  const parse = (text: string): bigint | null => {
    const match = /^(\d{1,12})(?:\.(\d{1,4}))?$/.exec(text.trim());
    return match === null ? null : BigInt(match[1]) * 10000n + BigInt((match[2] ?? '').padEnd(4, '0'));
  };
  const low = parse(draft.minimum), high = parse(draft.maximum), step = parse(draft.step);
  if (low === null || high === null || step === null || step <= 0n || (draft.key === 'no_reentry_after' ? low < 0n : low <= 0n) || high < low || (high - low) % step !== 0n || (high - low) / step >= 5000n) return null;
  const integer = draft.key === 'reentry_on_stop' || draft.key === 'no_reentry_after';
  if (integer && [low, high, step].some((value) => value % 10000n !== 0n)) return null;
  if (draft.key === 'reentry_on_stop' && high > 200000n) return null;
  if (draft.key === 'no_reentry_after' && high > 14390000n) return null;
  const values: SearchValue[] = [];
  for (let amount = low; amount <= high; amount += step) {
    const text = `${amount / 10000n}.${String(amount % 10000n).padStart(4, '0')}`;
    values.push(draft.key === 'reentry_on_stop' ? { kind: draft.kind, count: Number(amount / 10000n) } : draft.key === 'no_reentry_after' ? { anchor: draft.kind, offset_ms: Number(amount / 10000n) * 60000 } : { kind: draft.kind, value: text });
  }
  if (draft.off) values.push(null);
  return values.length >= 2 && values.length <= 5000 ? values : null;
}

export function ruleSummary(key: RuleKey, value: SearchValue): string {
  if (value === null) return 'Off';
  if (typeof value !== 'object') return String(value);
  if (key === 'no_reentry_after') return `${Number(value['offset_ms']) / 60000} minutes ${value['anchor'] === 'open' ? 'after open' : 'before close'}`;
  if (key === 'reentry_on_stop') return `${value['count']} ${value['count'] === 1 ? 're-entry' : 're-entries'}, ${value['kind'] === 'on_momentum' ? 'after momentum' : 'immediately after stop'}`;
  return `${String(value['value']).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '')}${String(value['kind']).startsWith('percent') ? '%' : ' points'}${key === 'momentum' ? String(value['kind']).endsWith('down') ? ' premium fall' : ' premium rise' : ' of option premium'}`;
}
