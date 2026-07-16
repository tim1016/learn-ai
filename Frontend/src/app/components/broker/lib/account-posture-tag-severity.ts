export function accountPostureTagSeverity(
  posture: string,
): "success" | "warn" | "danger" | "secondary" {
  if (/clean|ready|active/i.test(posture)) return "success";
  if (/degraded|warning|stale/i.test(posture)) return "warn";
  if (/blocked|error|failed|frozen|unsafe/i.test(posture)) return "danger";
  return "secondary";
}
