import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { RouterLink, ActivatedRoute, type ParamMap } from "@angular/router";
import { toSignal } from "@angular/core/rxjs-interop";

import analyticalMetricCatalog from "@repo-contracts/strategy-lab/analytical-metric-catalog-v1.json";

import { MetricReferenceEntryComponent } from "./metric-reference-entry.component";
import type { AnalyticalMetricCatalog, MetricProducer, MetricVariant } from "./analytical-metric-catalog.models";

const CATALOG: AnalyticalMetricCatalog = analyticalMetricCatalog;

export interface MetricContextRequest {
  metricId: string | null;
  variantId: string | null;
  producer: string | null;
}

export interface MetricContextResolution {
  variant: MetricVariant;
  warning: string | null;
}

/** Resolve a contextual link without manufacturing a producer from a label. */
export function resolveMetricContext(
  variants: MetricVariant[],
  request: MetricContextRequest,
): MetricContextResolution {
  const defaultVariant = variants.find((variant) => variant.producer === "platform") ?? variants[0];
  if (!defaultVariant) throw new Error("The analytical metric catalog must contain at least one variant.");
  if (!request.metricId) return { variant: defaultVariant, warning: null };

  const metricVariants = variants.filter((variant) => variant.metric_id === request.metricId);
  if (metricVariants.length === 0) {
    return { variant: defaultVariant, warning: "The requested metric is not documented; showing the default entry." };
  }

  const requestedVariant = request.variantId
    ? metricVariants.find((variant) => variant.variant_id === request.variantId)
    : undefined;
  if (requestedVariant && (!request.producer || requestedVariant.producer === request.producer)) {
    return { variant: requestedVariant, warning: null };
  }

  const producer = isMetricProducer(request.producer) ? request.producer : null;
  const producerVariant = producer
    ? metricVariants.find((variant) => variant.producer === producer)
    : undefined;
  if (producerVariant) return { variant: producerVariant, warning: request.variantId ? "The requested variant is unavailable; showing this producer's documented contract." : null };

  const metricDefault = metricVariants.find((variant) => variant.producer === "platform") ?? metricVariants[0];
  return {
    variant: metricDefault,
    warning: "The requested producer is not documented; showing the default contract for this metric.",
  };
}

@Component({
  selector: "app-strategy-lab-analytical-manual",
  imports: [MetricReferenceEntryComponent, RouterLink],
  templateUrl: "./strategy-lab-analytical-manual.component.html",
  styleUrl: "./strategy-lab-analytical-manual.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabAnalyticalManualComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly queryParams = toSignal(this.route.queryParamMap, { initialValue: this.route.snapshot.queryParamMap });

  readonly search = signal("");
  readonly resolution = computed(() => resolveMetricContext(CATALOG.variants, readRequest(this.queryParams())));
  readonly selectedVariant = computed(() => this.resolution().variant);
  readonly runId = computed(() => parseRunId(this.queryParams().get("run")));
  readonly alternative = computed(() => {
    const selected = this.selectedVariant();
    return CATALOG.variants.find((variant) => selected.alternative_variant_ids.includes(variant.variant_id)) ?? null;
  });
  readonly filteredVariants = computed(() => {
    const term = this.search().trim().toLocaleLowerCase();
    if (!term) return CATALOG.variants;
    return CATALOG.variants.filter((variant) => searchableText(variant).includes(term));
  });

  updateSearch(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) this.search.set(target.value);
  }
}

function readRequest(params: ParamMap): MetricContextRequest {
  return {
    metricId: params.get("metric"),
    variantId: params.get("variant"),
    producer: params.get("producer"),
  };
}

function parseRunId(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function isMetricProducer(value: string | null): value is MetricProducer {
  return value !== null && value.length > 0;
}

function searchableText(variant: MetricVariant): string {
  return [
    variant.label,
    variant.metric_id,
    variant.variant_id,
    variant.producer,
    variant.definition,
    variant.interpretation,
    variant.canonical_symbol,
    ...variant.aliases,
    ...variant.search_terms,
  ].join(" ").toLocaleLowerCase();
}
