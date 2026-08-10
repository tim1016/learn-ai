import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from "@angular/core";
import { RouterLink, ActivatedRoute, type ParamMap } from "@angular/router";
import { toSignal } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { firstValueFrom } from "rxjs";

import analyticalMetricCatalog from "@repo-contracts/strategy-lab/analytical-metric-catalog-v1.json";

import { MetricReferenceEntryComponent } from "./metric-reference-entry.component";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import type { AnalyticalMetricCatalog, MetricProducer, MetricVariant } from "./analytical-metric-catalog.models";
import {
  BACKTEST_RUN_DETAIL_QUERY,
  type BacktestRunDetailQueryResult,
  type MetricDocumentationContext,
} from "../../../graphql/backtest-runs.query";

const CATALOG: AnalyticalMetricCatalog = analyticalMetricCatalog;

export interface MetricContextRequest {
  metricId: string | null;
  variantId: string | null;
  producer: string | null;
  contractId: string | null;
}

export interface MetricContextResolution {
  variant: MetricVariant;
  warning: string | null;
}

export type MetricCategoryFilter = MetricVariant["category"] | "all";
export type MetricProducerFilter = MetricProducer | "all";

export interface MetricReferenceFilters {
  search: string;
  category: MetricCategoryFilter;
  producer: MetricProducerFilter;
  contextualVariantId: string | null;
  usedByThisRun: boolean;
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
    return {
      variant: requestedVariant,
      warning: contractWarning(requestedVariant, request.contractId),
    };
  }

  const producer = request.producer?.trim() || null;
  const producerVariant = producer
    ? metricVariants.find((variant) => variant.producer === producer)
    : undefined;
  if (producerVariant) {
    return {
      variant: producerVariant,
      warning: request.variantId
        ? "The requested variant is unavailable; showing this producer's documented contract."
        : contractWarning(producerVariant, request.contractId),
    };
  }

  const metricDefault = metricVariants.find((variant) => variant.producer === "platform") ?? metricVariants[0];
  return {
    variant: metricDefault,
    warning: request.contractId
      ? "The requested producer or contract is not documented; showing the default contract for this metric."
      : "The requested producer is not documented; showing the default contract for this metric.",
  };
}

export function filterMetricVariants(
  variants: MetricVariant[],
  filters: MetricReferenceFilters,
): MetricVariant[] {
  const term = filters.search.trim().toLocaleLowerCase();
  return variants.filter((variant) =>
    (!term || searchableText(variant).includes(term))
    && (filters.category === "all" || variant.category === filters.category)
    && (filters.producer === "all" || variant.producer === filters.producer)
    && (!filters.usedByThisRun || variant.variant_id === filters.contextualVariantId),
  );
}

@Component({
  selector: "app-strategy-lab-analytical-manual",
  imports: [MetricReferenceEntryComponent, ReceiptLabelPipe, RouterLink],
  templateUrl: "./strategy-lab-analytical-manual.component.html",
  styleUrl: "./strategy-lab-analytical-manual.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabAnalyticalManualComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly apollo = inject(Apollo);
  private readonly queryParams = toSignal(this.route.queryParamMap, { initialValue: this.route.snapshot.queryParamMap });

  readonly search = signal("");
  readonly category = signal<MetricCategoryFilter>("all");
  readonly producer = signal<MetricProducerFilter>("all");
  readonly usedByThisRun = signal(false);
  readonly categories = [...new Set(CATALOG.variants.map((variant) => variant.category))].sort();
  readonly producers = [...new Set(CATALOG.variants.map((variant) => variant.producer))].sort();
  readonly resolution = computed(() => resolveMetricContext(CATALOG.variants, readRequest(this.queryParams())));
  readonly selectedVariant = computed(() => this.resolution().variant);
  readonly runId = computed(() => parseRunId(this.queryParams().get("run")));
  readonly alternative = computed(() => {
    const selected = this.selectedVariant();
    return CATALOG.variants.find((variant) => selected.alternative_variant_ids.includes(variant.variant_id)) ?? null;
  });
  // A URL can name any run id and variant it likes; that alone does not mean
  // the run actually used that variant. Load the run's real GraphQL receipt
  // and only claim "used by this run" when the selected variant appears in it.
  private readonly runReceipt = resource({
    params: () => this.runId() ?? undefined,
    loader: async ({ params: runId }): Promise<MetricDocumentationContext[]> => {
      const response = await firstValueFrom(
        this.apollo.query<BacktestRunDetailQueryResult>({
          query: BACKTEST_RUN_DETAIL_QUERY,
          variables: { id: runId },
          fetchPolicy: "cache-first",
        }),
      );
      return response.data?.backtestRun?.metricDocumentation ?? [];
    },
  });
  readonly hasRunContext = computed(() => {
    // resource().value() throws (not just returns undefined) once the
    // resource has settled into an error state -- a transient GraphQL or
    // network failure must not crash this computed; treat it as "not
    // recorded" the same way a loading/idle resource already reads as.
    if (this.runReceipt.error()) return false;
    const receipt = this.runReceipt.value();
    if (!receipt) return false;
    const selected = this.selectedVariant();
    return receipt.some((context) => context.variantId === selected.variant_id);
  });
  readonly filteredVariants = computed(() => filterMetricVariants(CATALOG.variants, {
    search: this.search(),
    category: this.category(),
    producer: this.producer(),
    contextualVariantId: this.hasRunContext() ? this.selectedVariant().variant_id : null,
    usedByThisRun: this.usedByThisRun(),
  }));
  readonly activeFilterSummary = computed(() => {
    const filters = [
      this.search().trim() ? `search “${this.search().trim()}”` : null,
      this.category() === "all" ? null : `category ${this.category().replaceAll("_", " ")}`,
      this.producer() === "all" ? null : `producer ${this.producer().replaceAll("_", " ")}`,
      this.usedByThisRun() ? "used by this run" : null,
    ].filter((value): value is string => value !== null);
    return filters.join(", ");
  });

  updateSearch(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) this.search.set(target.value);
  }

  updateCategory(event: Event): void {
    const value = event.target instanceof HTMLSelectElement ? event.target.value : "all";
    this.category.set(value === "all" || this.categories.includes(value) ? value : "all");
  }

  updateProducer(event: Event): void {
    const value = event.target instanceof HTMLSelectElement ? event.target.value : "all";
    this.producer.set(value === "all" || this.producers.includes(value) ? value : "all");
  }

  updateUsedByThisRun(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.usedByThisRun.set(event.target.checked);
  }

  clearFilters(): void {
    this.search.set("");
    this.category.set("all");
    this.producer.set("all");
    this.usedByThisRun.set(false);
  }
}

function readRequest(params: ParamMap): MetricContextRequest {
  return {
    metricId: params.get("metric"),
    variantId: params.get("variant"),
    producer: params.get("producer"),
    contractId: params.get("contract"),
  };
}

function parseRunId(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function contractWarning(variant: MetricVariant, requestedContractId: string | null): string | null {
  if (!requestedContractId || requestedContractId === variant.contract_id) return null;
  return "The requested calculation contract is not available for this metric variant; review the contract shown below.";
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
    ...variant.source_keys,
    ...variant.search_terms,
  ].join(" ").toLocaleLowerCase();
}
