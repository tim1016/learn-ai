import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import type { AccountDeskRecoverySuccess } from "./account-desk-recovery-store.service";

interface RecoveryReceiptField {
  readonly label: string;
  readonly value: string;
  readonly kind: "code" | "label" | "text";
  readonly timestampMs: number | null;
}

interface RecoveryReceiptView {
  readonly message: string;
  readonly fields: readonly RecoveryReceiptField[];
}

/** Canonical display projection for each backend recovery receipt. */
@Component({
  selector: "app-account-desk-recovery-receipt",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: "./account-desk-recovery-receipt.component.html",
  styleUrl: "./account-desk-recovery-receipt.component.scss",
})
export class AccountDeskRecoveryReceiptComponent {
  readonly success = input.required<AccountDeskRecoverySuccess>();
  readonly receipt = computed(() => recoveryReceiptView(this.success()));
}

function field(
  label: string,
  value: string | number,
  kind: RecoveryReceiptField["kind"] = "text",
  timestampMs: number | null = null,
): RecoveryReceiptField {
  return { label, value: String(value), kind, timestampMs };
}

function recoveryReceiptView(success: AccountDeskRecoverySuccess): RecoveryReceiptView {
  switch (success.kind) {
    case "reconcile":
      return {
        message: "Account reconciliation returned a receipt.",
        fields: [
          field("Receipt", success.receipt.receipt_id, "code"),
          field("Recorded", "", "text", success.receipt.generated_at_ms),
          field("Gate", success.receipt.final_gate_result.status, "label"),
        ],
      };
    case "automation":
      return {
        message: "Auto-reconcile policy was updated.",
        fields: [
          field("Setting", success.policy.enabled ? "Enabled" : "Disabled"),
          field("Recorded", "", "text", success.policy.updated_at_ms),
          field("Operator", success.policy.updated_by, "code"),
        ],
      };
    case "clear_freeze":
      return {
        message: "Account freeze clear was accepted.",
        fields: [
          field("Recovery", success.receipt.recovery_id, "code"),
          field("Receipt", success.receipt.receipt_id, "code"),
          field("Gate", success.receipt.gate_result.status, "label"),
        ],
      };
    case "exposure_override":
      return {
        message: "Account exposure override was accepted.",
        fields: [
          field("Override", success.receipt.override_id, "code"),
          field("Account", success.receipt.account_id, "code"),
        ],
      };
    case "journal_cure":
      return {
        message: "Clerk journal cure was accepted.",
        fields: [
          field("Namespace", success.receipt.bot_order_namespace, "code"),
          field("Symbol", success.receipt.symbol, "code"),
          field("Sequence", success.receipt.journal_seq),
          field("Evidence", success.receipt.evidence_refs[0] ?? "", "code"),
          field("Recorded", "", "text", success.receipt.recorded_at_ms),
        ],
      };
    case "legacy_retire":
      return {
        message: "Legacy stale claim retirement was accepted.",
        fields: [
          field("Receipt", success.receipt.receipt_id, "code"),
          field("Strategy", success.receipt.strategy_instance_id, "code"),
          field("Run", success.receipt.run_id, "code"),
          field("Namespace", success.receipt.bot_order_namespace, "code"),
          field("Recorded", "", "text", success.receipt.retired_at_ms),
        ],
      };
    case "recovery_flatten":
      return {
        message: "Clerk recovery flatten was accepted.",
        fields: [
          field("Intent", success.receipt.recovery_flatten.recorded.intent_id, "code"),
          field("Order reference", success.receipt.recovery_flatten.recorded.order_ref, "code"),
          field("Order", success.receipt.recovery_flatten.broker_acked.order_id, "code"),
          field("Recorded", "", "text", success.receipt.recovery_flatten.broker_acked.recorded_at_ms),
        ],
      };
    case "emergency_flatten":
      return {
        message: "Emergency paper account flatten completed.",
        fields: [
          field("Account", success.receipt.account_id, "code"),
          field("Audit run", success.receipt.audit_run_id, "code"),
          field("Completed", "", "text", success.receipt.completed_at_ms),
        ],
      };
    case "restore_clerk":
      return {
        message: "Account Clerk restore completed.",
        fields: [
          field("Receipt", success.receipt.receipt_id, "code"),
          field("Clerk generation", success.receipt.clerk_generation),
          field("Recorded", "", "text", success.receipt.recorded_at_ms),
        ],
      };
    case "journal_recovery":
      return {
        message: success.receipt.phase === "COMPLETE"
          ? "Clerk journal was re-baselined from fresh broker evidence."
          : "Corrupt Clerk journal was quarantined as retained audit evidence.",
        fields: [
          field("Receipt", success.receipt.receipt_id, "code"),
          field("Quarantined journal", success.receipt.quarantined_journal_name ?? "", "code"),
          field("Phase", success.receipt.phase, "label"),
          field("Broker-evidence holdings", success.receipt.broker_evidence_positions.length),
          field("Recorded", "", "text", success.receipt.recorded_at_ms),
        ],
      };
  }
}
