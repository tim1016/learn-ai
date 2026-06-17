import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type {
  InstanceBrokerView,
  InstanceSizing,
  ReadinessVector,
} from '../../../../api/live-instances.types';

interface PositionRow {
  symbol: string;
  qty: number;
  side: 'long' | 'short';
}

type Posture = 'flat' | 'long' | 'short' | 'mixed' | 'unknown';

interface OrdersGate {
  /** True when the engine has emitted a typed orders_cap gate. */
  present: boolean;
  detail: string;
  status: 'pass' | 'fail' | 'unknown';
}

/**
 * "Current Risk" — the operator-priority answer to *"what is this bot holding
 * right now, and how close is it to the daily safety cap?"*.
 *
 * Issue #565 PR 9 (User Stories #17 + #18). Reads only fields already emitted
 * by the status contract — owned positions and pending order count from the
 * broker slice (#398), the daily-cap status from the readiness vector's
 * `orders_cap` gate (no fresh number-extraction from prose). When the engine
 * hasn't emitted a typed cap, the card is honest: a single "Daily cap status
 * not reported by the engine" line, not a fabricated counter.
 *
 * Sizing presence is folded in as a one-line summary that defers detail to
 * the existing sizing-card — this card is the posture-and-cap view; sizing
 * provenance belongs on the dedicated card.
 */
@Component({
  selector: 'app-current-risk-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './current-risk-card.component.html',
  styleUrl: './current-risk-card.component.scss',
})
export class CurrentRiskCardComponent {
  readonly broker = input.required<InstanceBrokerView | null>();
  readonly readiness = input.required<ReadinessVector | null>();
  readonly sizing = input.required<InstanceSizing | null>();

  readonly positions = computed<PositionRow[]>(() => {
    const b = this.broker();
    if (!b) return [];
    return Object.entries(b.owned_positions)
      .filter(([, qty]) => qty !== 0)
      .map<PositionRow>(([symbol, qty]) => ({
        symbol,
        qty,
        side: qty > 0 ? 'long' : 'short',
      }))
      .sort((a, b) => a.symbol.localeCompare(b.symbol));
  });

  readonly pendingOrderCount = computed<number>(() => {
    const b = this.broker();
    return b?.pending_order_count ?? 0;
  });

  readonly posture = computed<Posture>(() => {
    const b = this.broker();
    if (!b) return 'unknown';
    const rows = this.positions();
    if (rows.length === 0) return 'flat';
    const sides = new Set(rows.map((r) => r.side));
    if (sides.size > 1) return 'mixed';
    return rows[0].side;
  });

  readonly postureLabel = computed<string>(() => {
    const p = this.posture();
    const n = this.positions().length;
    switch (p) {
      case 'flat':
        return 'Flat · 0 positions';
      case 'long':
        return `Long · ${pluralize(n, 'position')}`;
      case 'short':
        return `Short · ${pluralize(n, 'position')}`;
      case 'mixed':
        return `Mixed · ${pluralize(n, 'position')}`;
      default:
        return 'Position posture unknown';
    }
  });

  readonly ordersGate = computed<OrdersGate>(() => {
    const r = this.readiness();
    const gate = r?.gates.find((g) => g.name === 'orders_cap');
    if (!gate) {
      return {
        present: false,
        detail: 'Daily cap status not reported by the engine.',
        status: 'unknown',
      };
    }
    return {
      present: true,
      detail: gate.detail,
      status: gate.status,
    };
  });

  readonly sizingSummary = computed<string>(() => {
    const s = this.sizing();
    if (!s) return 'Sizing not recorded';
    if (s.preset === null) return 'Pre-policy run (legacy ledger)';
    return formatPreset(s.preset);
  });

  readonly namespaceCaption = computed<string>(() => {
    const b = this.broker();
    return b?.bot_order_namespace ?? '';
  });
}

function pluralize(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

function formatPreset(preset: string): string {
  if (preset === 'explicit') return 'Explicit (strategy-defined)';
  return preset
    .split('_')
    .map((w) => (w.length ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}
