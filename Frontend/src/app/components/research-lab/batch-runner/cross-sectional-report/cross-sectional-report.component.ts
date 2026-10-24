import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { CardModule } from 'primeng/card';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { TooltipModule } from 'primeng/tooltip';

import {
  AggregateIcCi,
  BatchResearchResult,
  BinomialNullTest,
  CrossSectionalStageInfo,
  TickerBatchResult,
  TickerValidity,
  ValiditySummary,
} from '../../../../services/research.service';
import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';

/** The cross-sectional study's result view: verdict, aggregate cards, binomial row, per-ticker table. */
@Component({
  selector: 'app-cross-sectional-report',
  imports: [CommonModule, TableModule, TagModule, TooltipModule, CardModule, AssetIdentityComponent],
  templateUrl: './cross-sectional-report.component.html',
  styleUrl: './cross-sectional-report.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CrossSectionalReportComponent {
  readonly result = input.required<BatchResearchResult>();

  get passRatePct(): number {
    return this.result().passRate * 100;
  }

  get consistentSeverity(): 'success' | 'danger' {
    return this.result().crossSectionalConsistent ? 'success' : 'danger';
  }

  get consistentLabel(): string {
    return this.result().crossSectionalConsistent ? 'Consistent' : 'Not Consistent';
  }

  // ─── Verdict helpers (new schema only) ───────────────────────────

  /** Stage 0/1/2/3 from the SSE-driven response. ``null`` when the
   *  response doesn't populate ``stageInfo``. */
  readonly stage = computed<0 | 1 | 2 | 3 | null>(
    () => this.result().stageInfo?.stage ?? null,
  );

  readonly stageBand = computed<'green' | 'amber' | 'red' | 'na'>(() => {
    const s = this.stage();
    if (s === null) return 'na';
    if (s === 3) return 'green';
    if (s === 2) return 'green';
    if (s === 1) return 'amber';
    return 'red';
  });

  readonly aggregateIcCi = computed<AggregateIcCi | null>(
    () => this.result().aggregateIcCi ?? null,
  );

  readonly binomialTest = computed<BinomialNullTest | null>(
    () => this.result().binomialTest ?? null,
  );

  readonly validitySummary = computed<ValiditySummary | null>(
    () => this.result().validitySummary ?? null,
  );

  readonly stageInfo = computed<CrossSectionalStageInfo | null>(
    () => this.result().stageInfo ?? null,
  );

  readonly ciStraddlesZero = computed<boolean>(() => {
    const ci = this.aggregateIcCi();
    return !!ci?.valid && ci.ciLower < 0 && ci.ciUpper > 0;
  });

  /** "0.0281 ± 0.0156" form for the headline; falls back to "0.0281"
   *  when the CI was not computed (legacy path). */
  readonly aggregateIcDisplay = computed<string>(() => {
    const r = this.result();
    const ci = this.aggregateIcCi();
    if (ci?.valid) {
      const halfWidth = (ci.ciUpper - ci.ciLower) / 2;
      return `${ci.point.toFixed(4)} ± ${halfWidth.toFixed(4)}`;
    }
    return r.aggregateIc.toFixed(4);
  });

  readonly ciIntervalDisplay = computed<string | null>(() => {
    const ci = this.aggregateIcCi();
    if (!ci?.valid) return null;
    return `95% CI [${ci.ciLower.toFixed(4)}, ${ci.ciUpper.toFixed(4)}]`;
  });

  /** Per-ticker UI tag — distinguishes PASS / FAIL / INVALID / ERROR. */
  validityTagSeverity(row: TickerBatchResult): 'success' | 'danger' | 'warn' | 'info' {
    const v: TickerValidity = row.validity ?? 'valid';
    if (v === 'invalid_iv' || v === 'invalid_data') return 'warn';
    if (v === 'error') return 'info';
    return row.passedValidation ? 'success' : 'danger';
  }

  validityTagLabel(row: TickerBatchResult): string {
    const v: TickerValidity = row.validity ?? 'valid';
    if (v === 'invalid_iv') return 'INVALID — IV';
    if (v === 'invalid_data') return 'INVALID — DATA';
    if (v === 'error') return 'ERROR';
    if (row.lowConfidence) {
      return row.passedValidation ? 'PASS · low conf' : 'FAIL · low conf';
    }
    return row.passedValidation ? 'PASS' : 'FAIL';
  }

  /** True when the CI half-width exceeds |IC| by a wide margin —
   *  i.e. the band is much larger than the point estimate, so the CI
   *  is technically computable but not interpretable. Used to render
   *  a banner under the headline. */
  readonly ciUnreliable = computed<boolean>(() => {
    const ci = this.aggregateIcCi();
    if (!ci?.valid) return false;
    const halfWidth = Math.abs(ci.ciUpper - ci.ciLower) / 2;
    const magnitude = Math.abs(ci.point);
    // Heuristic: half-width > 5× |IC| means the CI is essentially
    // uninformative even though the math went through.
    return magnitude > 0 && halfWidth > 5 * magnitude;
  });

  /** Disclosure label for which input matrix drove `nEffAssets`. */
  readonly nEffAssetsMethodLabel = computed<string>(() => {
    const m = this.result().nEffAssetsMethod;
    if (m === 'ic') return 'IC time series';
    if (m === 'returns') return 'daily stock returns (Stage-1 fallback)';
    return 'unknown';
  });

  getIcClass(ic: number): string {
    const abs = Math.abs(ic);
    if (abs >= 0.05) return 'text-green-500 font-bold';
    if (abs >= 0.03) return 'text-yellow-500';
    return 'text-red-500';
  }
}
