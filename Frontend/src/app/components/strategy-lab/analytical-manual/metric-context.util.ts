import type { MetricVariant } from "./analytical-metric-catalog.models";

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

  const producer = request.producer?.trim() || null;
  const requestedVariant = request.variantId
    ? metricVariants.find((variant) => variant.variant_id === request.variantId)
    : undefined;
  if (requestedVariant && (!producer || requestedVariant.producer === producer)) {
    return {
      variant: requestedVariant,
      warning: contractWarning(requestedVariant, request.contractId),
    };
  }

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

function contractWarning(variant: MetricVariant, requestedContractId: string | null): string | null {
  if (!requestedContractId || requestedContractId === variant.contract_id) return null;
  return "The requested calculation contract is not available for this metric variant; review the contract shown below.";
}
