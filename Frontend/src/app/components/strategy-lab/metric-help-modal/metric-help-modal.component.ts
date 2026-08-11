import { ChangeDetectionStrategy, Component, computed, input, signal } from "@angular/core";
import { RouterLink } from "@angular/router";
import { Dialog } from "primeng/dialog";

import analyticalMetricCatalog from "@repo-contracts/strategy-lab/analytical-metric-catalog-v1.json";

import type { MetricDocumentationContext } from "../../../graphql/backtest-runs.query";
import type { AnalyticalMetricCatalog } from "../analytical-manual/analytical-metric-catalog.models";
import { resolveMetricContext } from "../analytical-manual/metric-context.util";
import { MetricReferenceEntryComponent } from "../analytical-manual/metric-reference-entry.component";

const CATALOG: AnalyticalMetricCatalog = analyticalMetricCatalog;

/** Opens the complete backend-authored metric guide without leaving Results. */
@Component({
  selector: "app-metric-help-modal",
  imports: [Dialog, MetricReferenceEntryComponent, RouterLink],
  templateUrl: "./metric-help-modal.component.html",
  styleUrl: "./metric-help-modal.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MetricHelpModalComponent {
  readonly metricId = input<string | null>(null);
  readonly variantId = input<string | null>(null);
  readonly label = input.required<string>();
  readonly context = input<MetricDocumentationContext | null>(null);
  readonly runId = input<number | null>(null);
  readonly visible = signal(false);

  readonly resolution = computed(() => {
    const requestedVariant = this.variantId()
      ? CATALOG.variants.find((variant) => variant.variant_id === this.variantId())
      : null;
    if (requestedVariant) return { variant: requestedVariant, warning: null };

    const metricId = this.metricId();
    const context = this.context()?.metricId === metricId ? this.context() : null;
    return resolveMetricContext(CATALOG.variants, {
      metricId,
      variantId: context?.variantId ?? null,
      producer: context?.producer ?? null,
      contractId: context?.contractId ?? null,
    });
  });
  readonly variant = computed(() => this.resolution().variant);
  readonly alternative = computed(() => {
    const variant = this.variant();
    return CATALOG.variants.find((candidate) => variant.alternative_variant_ids.includes(candidate.variant_id)) ?? null;
  });
  readonly docsQuery = computed(() => {
    const variant = this.variant();
    return {
      metric: variant.metric_id,
      variant: variant.variant_id,
      producer: variant.producer,
      contract: variant.contract_id ?? undefined,
      run: this.runId() ?? undefined,
    };
  });

  open(): void {
    this.visible.set(true);
  }

  onVisibleChange(visible: boolean): void {
    this.visible.set(visible);
  }
}
