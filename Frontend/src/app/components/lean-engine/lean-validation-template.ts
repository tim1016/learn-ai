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
 * The registry response is authoritative. The key fallback keeps the two
 * known validation strategies runnable while a frontend is deployed before
 * the Python strategy-catalog service is restarted with the new field.
 */
export function leanValidationTemplateForStrategy(
  strategyKey: string,
  declaredTemplate: string | null | undefined,
): LeanValidationTemplate | null {
  if (isLeanValidationTemplate(declaredTemplate)) {
    return declaredTemplate;
  }
  return LEGACY_STRATEGY_TEMPLATES[strategyKey] ?? null;
}
