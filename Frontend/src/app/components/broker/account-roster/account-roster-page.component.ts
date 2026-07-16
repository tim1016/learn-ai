import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from "@angular/core";
import { Router } from "@angular/router";
import { ButtonModule } from "primeng/button";
import { MessageModule } from "primeng/message";
import { SkeletonModule } from "primeng/skeleton";
import { TableModule } from "primeng/table";
import { TagModule } from "primeng/tag";

import type { AccountRosterRow } from "../../../api/account-directory.types";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import { AccountDeskDirectoryStore } from "../account-desk/account-desk-directory-store.service";
import { accountPostureTagSeverity } from "../lib/account-posture-tag-severity";

/** Entry roster for the account-keyed desk. */
@Component({
  selector: "app-account-roster-page",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ButtonModule,
    MessageModule,
    ReceiptLabelPipe,
    SkeletonModule,
    TableModule,
    TagModule,
    TimestampDisplayComponent,
  ],
  templateUrl: "./account-roster-page.component.html",
  styleUrl: "./account-roster-page.component.scss",
})
export class AccountRosterPageComponent {
  readonly directory = inject(AccountDeskDirectoryStore);
  private readonly router = inject(Router);
  readonly skeletonRows = [0, 1, 2];
  readonly rosterRows = computed(() => [...this.directory.rosterRows()]);
  readonly postureSeverity = accountPostureTagSeverity;
  readonly selectedAccount = signal<AccountRosterRow | null>(null);

  constructor() {
    void this.directory.loadRoster();
  }

  openDesk(row: AccountRosterRow): void {
    void this.router.navigate(["/broker/accounts", row.account_id]);
  }

  openSelectedDesk(
    row: AccountRosterRow | AccountRosterRow[] | undefined,
  ): void {
    if (row === undefined || Array.isArray(row)) return;
    this.openDesk(row);
  }

  retry(): void {
    this.directory.retryRoster();
  }
}
