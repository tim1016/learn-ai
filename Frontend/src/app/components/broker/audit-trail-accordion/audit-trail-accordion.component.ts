import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import {
  Accordion,
  AccordionContent,
  AccordionHeader,
  AccordionPanel,
} from 'primeng/accordion';
import type { InstanceProvenance } from '../../../api/live-instances.types';

interface ProofRow {
  label: string;
  statement: string;
  mono: string | null;
}

/**
 * Audit & Diagnostics — the page-level provenance accordion. Holds the run's
 * content-addressed identity (run_id + hashed deploy inputs) as plain-language
 * proof statements, with the full fingerprints behind a sub-disclosure.
 *
 * Per the #565 refactor (PR 4), the previous `broker-provenance-card` is
 * wrapped in `p-accordion` and collapsed by default so engineering data lives
 * one click away rather than dominating the trader's default view.
 *
 * VCR-0014 / Phase 7D — the QC provenance row is split into two distinct
 * proofs. The audit copy is verifiable (SHA against the on-disk file +
 * ADR 0009 allow-list verdict). The QC Cloud backtest id is operator-
 * recorded — no automated verification path against QC Cloud exists yet, so
 * the row labels itself "Operator-recorded, not auto-verified" rather than
 * the forbidden "QC-approved" / "verified backtest" / "Byte-identical to
 * backtest" copy.
 */
@Component({
  selector: 'app-audit-trail-accordion',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, Accordion, AccordionPanel, AccordionHeader, AccordionContent],
  templateUrl: './audit-trail-accordion.component.html',
  styleUrl: './audit-trail-accordion.component.scss',
})
export class AuditTrailAccordionComponent {
  readonly provenance = input.required<InstanceProvenance>();

  readonly runIdShort = computed<string>(() => shortSha(this.provenance().run_id));

  readonly proofs = computed<ProofRow[]>(() => {
    const p = this.provenance();
    const rows: ProofRow[] = [];
    if (p.code_sha) {
      rows.push({
        label: 'Code',
        statement: 'Ran this committed code — the working tree was verified clean at deploy.',
        mono: shortSha(p.code_sha),
      });
    }
    if (p.strategy_spec_path || p.strategy_spec_sha256) {
      rows.push({
        label: 'Strategy contract',
        statement: `Spec ${filename(p.strategy_spec_path) || '(recorded)'}`,
        mono: shortSha(p.strategy_spec_sha256) || null,
      });
    }
    // VCR-0014 / Phase 7D — split the old "QC-approved /
    // Byte-identical to backtest" row (which conflated a verifiable SHA
    // claim with an operator-recorded id) into two honest proofs.
    if (p.qc_audit_copy_sha256 || p.qc_audit_copy_path) {
      rows.push({
        label: 'Audit copy',
        statement: `SHA recorded for ${filename(p.qc_audit_copy_path) || '(recorded)'}`,
        mono: shortSha(p.qc_audit_copy_sha256) || null,
      });
    }
    if (p.qc_cloud_backtest_id) {
      rows.push({
        label: 'QC Cloud backtest',
        statement: `${p.qc_cloud_backtest_id} — Operator-recorded, not auto-verified.`,
        mono: null,
      });
    }
    if (p.account_id) {
      rows.push({ label: 'Account', statement: p.account_id, mono: null });
    }
    const config = configSummary(p.live_config);
    if (config) {
      rows.push({ label: 'Runtime config', statement: config, mono: null });
    }
    return rows;
  });
}

function configSummary(config: Record<string, unknown>): string {
  return Object.entries(config)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(', ');
}

function shortSha(value: string): string {
  return value ? value.slice(0, 12) : '';
}

function filename(path: string): string {
  if (!path) return '';
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}
