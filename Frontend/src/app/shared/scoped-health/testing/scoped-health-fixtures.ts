import type { Provider } from '@angular/core';
import { vi } from 'vitest';

import type { HealthSeverity, ScopedHealth, ScopedHealthItem } from '../scoped-health';
import { ScopedHealthService, type ScopedHealthQuery } from '../scoped-health.service';

type Kind = ScopedHealthItem['scope']['data_kind'];

/** What the data plane says about each data kind: its source, and the operations it can get in the way of. */
const KINDS: Readonly<Record<Kind, Pick<ScopedHealthItem, 'source' | 'affects'>>> = {
  stock_bars: { source: 'lake_catalog', affects: ['stock_history'] },
  option_archive: { source: 'option_archive', affects: ['options_history'] },
  lake_backup: { source: 'backups', affects: ['recovery'] },
  restore_check: { source: 'backups', affects: ['recovery'] },
  ibkr_market_data: { source: 'ibkr_feed', affects: ['live_chain', 'bot_feed'] },
};

/** The same call the data plane makes: these three are the ones that deserve a note on a page. */
const ASKS_FOR_ATTENTION: readonly HealthSeverity[] = ['warning', 'critical', 'unknown'];

export const OBSERVED_MS = 1_792_782_000_000;

interface ItemSpec {
  readonly kind: Kind;
  readonly severity?: HealthSeverity;
  readonly instrument?: string;
  readonly summary?: string;
  readonly explanation?: string;
  readonly link?: ScopedHealthItem['link'];
  readonly freshness?: ScopedHealthItem['freshness'];
}

/** One item as the data plane sends it. Healthy unless a severity is given. */
export function healthItem(spec: ItemSpec): ScopedHealthItem {
  const severity = spec.severity ?? 'ok';
  return {
    ...KINDS[spec.kind],
    scope: {
      data_kind: spec.kind,
      instrument: spec.instrument ?? null,
      breadth: spec.instrument === undefined ? 'broad' : 'scoped',
      session_ms: null,
      mode: null,
    },
    observed_ms: OBSERVED_MS,
    freshness: spec.freshness ?? (severity === 'unknown' ? 'unknown' : 'fresh'),
    severity,
    needs_attention: ASKS_FOR_ATTENTION.includes(severity),
    summary: spec.summary ?? `${spec.kind} is ${severity}`,
    explanation: spec.explanation ?? `${spec.kind} is ${severity}.`,
    link: spec.link ?? null,
  };
}

/** Worst first, as the data plane sends them. */
const WORST_FIRST: readonly HealthSeverity[] = ['critical', 'unknown', 'warning', 'info', 'ok'];

export function projection(...items: ScopedHealthItem[]): ScopedHealth {
  return {
    generated_ms: OBSERVED_MS,
    items: [...items].sort((a, b) => WORST_FIRST.indexOf(a.severity) - WORST_FIRST.indexOf(b.severity)),
  };
}

/** Every source healthy, each with the link the data plane gives it. */
export function allHealthy(): ScopedHealth {
  return projection(
    healthItem({ kind: 'stock_bars', link: { tab: 'lake', query: {} } }),
    healthItem({ kind: 'option_archive', instrument: 'SPY', link: { tab: 'option-archive', query: {}, fragment: 'option-archive-SPY' } }),
    healthItem({ kind: 'lake_backup', link: { tab: 'backups', query: {}, fragment: 'option-archive-backup' } }),
    healthItem({ kind: 'restore_check', link: { tab: 'backups', query: {}, fragment: 'option-archive-restore' } }),
    healthItem({ kind: 'ibkr_market_data' }),
  );
}

/** A fake projection reader answering every question with `answer` (or failing, when it is an error). */
export function fakeScopedHealth(answer: ScopedHealth | Error | ((query: ScopedHealthQuery) => ScopedHealth)) {
  const read = vi.fn(async (query: ScopedHealthQuery = {}): Promise<ScopedHealth> => {
    if (answer instanceof Error) throw answer;
    return typeof answer === 'function' ? answer(query) : answer;
  });
  const provider: Provider = { provide: ScopedHealthService, useValue: { read } };
  return { read, provider };
}
