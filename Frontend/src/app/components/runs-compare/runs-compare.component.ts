import { CurrencyPipe, DecimalPipe, PercentPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';
import { of } from 'rxjs';
import { RunsCompareService } from '../../services/runs-compare.service';
import type { CompareResponse } from '../../models/compare-response';

interface ParsedIds {
  left: number;
  right: number;
}

/**
 * PR B (2026-05-19) Phase 4 — ``/runs/compare`` view.
 *
 * Renders the compatibility verdict (multi-claim header per spec § 7.3),
 * the summary-card deltas, the first-divergence callout, the trade-by-
 * trade diff table, and the raw-run-links section.  State-trace section
 * is conditionally rendered based on ``state_trace_available``; hidden in
 * v1 because the workspace-path column isn't wired through yet.
 *
 * Data flow:
 *   route query params (?left=&right=) → resource params
 *   → RunsCompareService.getCompare → CompareResponse signal
 */
@Component({
  selector: 'app-runs-compare',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DecimalPipe, PercentPipe],
  templateUrl: './runs-compare.component.html',
  styleUrl: './runs-compare.component.scss',
})
export class RunsCompareComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly svc = inject(RunsCompareService);

  protected readonly compareResource = rxResource<CompareResponse | null, ParsedIds | null>({
    params: () => this.parsedIdsFromRoute(),
    stream: ({ params }) => {
      if (params === null) {
        return of(null);
      }
      return this.svc.getCompare(params.left, params.right);
    },
  });

  protected readonly data = computed(() => this.compareResource.value() ?? null);
  protected readonly isLoading = computed(() => this.compareResource.isLoading());
  protected readonly hasIds = computed(() => this.parsedIdsFromRoute() !== null);
  protected readonly errorMessage = computed(() => {
    const err = this.compareResource.error();
    if (!err) return null;
    return err instanceof Error ? err.message : String(err);
  });

  // Sub-claim signals for the compatibility header (see spec § 7.3).
  protected readonly dataPolicyClaim = computed<ClaimVerdict>(() => {
    const d = this.data();
    if (!d) return 'unknown';
    const fields = ['symbol', 'session', 'adjusted', 'input_bars', 'strategy_bars', 'data_policy_missing', 'window'];
    return d.mismatches.some((m) => fields.includes(m)) ? 'mismatch' : 'match';
  });

  protected readonly runParamsClaim = computed<ClaimVerdict>(() => {
    const d = this.data();
    if (!d) return 'unknown';
    const fields = ['starting_cash', 'commission_per_order', 'fill_mode'];
    return d.mismatches.some((m) => fields.includes(m)) ? 'mismatch' : 'match';
  });

  protected readonly brokerageClaim = computed<ClaimVerdict>(() => {
    const d = this.data();
    if (!d) return 'unknown';
    if (d.mismatches.includes('brokerage_policy')) return 'mismatch';
    if (d.informational_differences.includes('brokerage_policy')) return 'soft';
    return 'match';
  });

  /**
   * Trade-diff rows the table renders. Matched pairs come first in their
   * natural order; python_only and lean_only append at the end so the
   * eye can scan for asymmetries.
   */
  protected readonly tradeRows = computed<TradeRow[]>(() => {
    const d = this.data();
    if (!d) return [];
    const rows: TradeRow[] = [];
    for (const p of d.trade_diff.matched_pairs) {
      rows.push({
        kind: 'matched',
        tradeNumber: p.trade_number,
        entryDeltaMs: p.entry_ts_delta_ms,
        exitDeltaMs: p.exit_ts_delta_ms,
        entryPriceDelta: p.entry_price_delta,
        exitPriceDelta: p.exit_price_delta,
        qtyDelta: p.qty_delta,
        pnlDelta: p.pnl_delta,
        category: p.category,
      });
    }
    for (const t of d.trade_diff.python_only) {
      rows.push({
        kind: 'python_only',
        tradeNumber: t.trade_number,
        entryDeltaMs: 0,
        exitDeltaMs: 0,
        entryPriceDelta: '',
        exitPriceDelta: '',
        qtyDelta: '',
        pnlDelta: t.pnl,
        category: 'python_only',
      });
    }
    for (const t of d.trade_diff.lean_only) {
      rows.push({
        kind: 'lean_only',
        tradeNumber: t.trade_number,
        entryDeltaMs: 0,
        exitDeltaMs: 0,
        entryPriceDelta: '',
        exitPriceDelta: '',
        qtyDelta: '',
        pnlDelta: t.pnl,
        category: 'lean_only',
      });
    }
    return rows;
  });

  protected readonly mismatchListLabel = computed(() => {
    const d = this.data();
    if (!d || d.mismatches.length === 0) return '';
    return d.mismatches.join(', ');
  });

  private parsedIdsFromRoute(): ParsedIds | null {
    const map = this.route.snapshot.queryParamMap;
    const left = Number.parseInt(map.get('left') ?? '', 10);
    const right = Number.parseInt(map.get('right') ?? '', 10);
    if (!Number.isFinite(left) || !Number.isFinite(right)) {
      return null;
    }
    return { left, right };
  }
}

type ClaimVerdict = 'match' | 'mismatch' | 'soft' | 'unknown';

interface TradeRow {
  kind: 'matched' | 'python_only' | 'lean_only';
  tradeNumber: number;
  entryDeltaMs: number;
  exitDeltaMs: number;
  entryPriceDelta: string;
  exitPriceDelta: string;
  qtyDelta: string;
  pnlDelta: string;
  category: string;
}
