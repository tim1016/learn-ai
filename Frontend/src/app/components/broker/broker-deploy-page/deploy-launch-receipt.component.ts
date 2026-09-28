import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, ElementRef, computed, input, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { AccountWorkspaceLink } from '../../../fleet/account-workspace';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';
import { DEPLOY_WORLD_WORDING, deployWorldOf } from './deploy-world';

/**
 * What one Deploy recorded: the bot the backend named, the money set aside,
 * the world, and — from Deploy again — the bot it replaces. A Dry Run's cash
 * is its own, so its receipt never names the real account (H18).
 */
@Component({
  selector: 'app-deploy-launch-receipt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, ReceiptLabelPipe, RouterLink, TimestampDisplayComponent],
  templateUrl: './deploy-launch-receipt.component.html',
  styleUrl: './deploy-launch-receipt.component.scss',
})
export class DeployLaunchReceiptComponent {
  readonly receipt = input.required<BudgetDeployReceipt>();
  /** The new bot's own page in this workspace, when the workspace has one. */
  readonly botLink = input<AccountWorkspaceLink | null>(null);

  protected readonly dryRun = computed(() => this.receipt().world === 'synthetic');
  protected readonly worldWording = computed(() => DEPLOY_WORLD_WORDING[deployWorldOf(this.receipt().world)]);

  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  /** Moves the keyboard to the outcome after a Deploy. */
  focus(): void {
    this.panel().nativeElement.focus();
  }
}
