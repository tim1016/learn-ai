import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import {
  Accordion,
  AccordionContent,
  AccordionHeader,
  AccordionPanel,
} from 'primeng/accordion';

import {
  ReceiptLabelPipe,
  formatReceiptValue,
} from '../../../shared/pipes/receipt-label.pipe';
import type { DeployReadinessCheck } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-readiness-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Accordion, AccordionContent, AccordionHeader, AccordionPanel, ReceiptLabelPipe],
  templateUrl: './deploy-readiness-section.component.html',
  styleUrl: './deploy-readiness-section.component.scss',
})
export class DeployReadinessSectionComponent {
  readonly checks = input.required<DeployReadinessCheck[]>();

  /** Blocked gates open by default; ready gates stay collapsed until the operator opens them. */
  protected readonly defaultOpenGateIds = computed(() =>
    this.checks().filter((check) => !check.ready).map((check) => check.gate_id),
  );

  protected evidenceEntries(
    check: DeployReadinessCheck,
  ): [string, string][] {
    return Object.entries(check.evidence ?? {}).map(([label, value]) => [
      label,
      formatReceiptValue(label, value === null ? 'Not recorded' : value),
    ]);
  }
}
