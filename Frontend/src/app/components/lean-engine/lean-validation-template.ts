import type { TrustedRunRequest } from '../../services/lean-sidecar.types';

export type LeanValidationTemplate = Extract<
  NonNullable<TrustedRunRequest['template']>,
  'ema_crossover_signal' | 'deployment_validation'
>;

const TEMPLATE_LABELS: Readonly<Record<LeanValidationTemplate, string>> = {
  ema_crossover_signal: 'EMA Crossover Signal',
  deployment_validation: 'Deployment Validation',
};

export function isLeanValidationTemplate(
  template: string | null | undefined,
): template is LeanValidationTemplate {
  return template === 'ema_crossover_signal' || template === 'deployment_validation';
}

export function leanValidationTemplateLabel(template: LeanValidationTemplate): string {
  return TEMPLATE_LABELS[template];
}
