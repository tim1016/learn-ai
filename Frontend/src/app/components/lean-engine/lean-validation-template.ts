import type { TrustedRunRequest } from '../../services/lean-sidecar.types';

export type LeanValidationTemplate = Extract<
  NonNullable<TrustedRunRequest['template']>,
  'ema_crossover_signal' | 'deployment_validation'
>;

const TEMPLATE_LABELS: Readonly<Record<LeanValidationTemplate, string>> = {
  ema_crossover_signal: 'EMA Crossover Signal',
  deployment_validation: 'Deployment Validation',
};

const LEGACY_STRATEGY_TEMPLATES: Readonly<Record<string, LeanValidationTemplate>> = {
  ema_crossover_signal: 'ema_crossover_signal',
  spy_ema_crossover: 'ema_crossover_signal',
  deployment_validation: 'deployment_validation',
};

export function isLeanValidationTemplate(
  template: string | null | undefined,
): template is LeanValidationTemplate {
  return template === 'ema_crossover_signal' || template === 'deployment_validation';
}

export function leanValidationTemplateLabel(template: LeanValidationTemplate): string {
  return TEMPLATE_LABELS[template];
}

/**
 * The registry response is authoritative whenever it declares the field.
 * The key fallback only supports an older strategy-catalog service that
 * omits ``lean_twin`` during a rolling frontend deployment.
 */
export function leanValidationTemplateForStrategy(
  strategyKey: string,
  declaredTemplate: string | null | undefined,
): LeanValidationTemplate | null {
  if (declaredTemplate !== undefined) {
    return isLeanValidationTemplate(declaredTemplate) ? declaredTemplate : null;
  }
  return LEGACY_STRATEGY_TEMPLATES[strategyKey] ?? null;
}
