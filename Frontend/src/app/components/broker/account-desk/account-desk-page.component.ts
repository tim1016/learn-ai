import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  signal,
} from "@angular/core";
import { takeUntilDestroyed } from "@angular/core/rxjs-interop";
import { ActivatedRoute, Router } from "@angular/router";
import { ButtonModule } from "primeng/button";
import { CardModule } from "primeng/card";
import { MessageModule } from "primeng/message";
import { PanelModule } from "primeng/panel";

import type { AccountTriageVerdictMove } from "../../../api/account-reconciliation.types";
import type { AccountDeskLens } from "../../../api/operator-blocker.types";
import { PageHeaderComponent } from "../../../shared/page-header/page-header.component";
import { fmtDurationRemaining } from "../format";
import { AccountDeskHoldingsStore } from "./account-desk-holdings-store.service";
import {
  AccountDeskLensSelectComponent,
  type AccountDeskLensOption,
} from "./account-desk-lens-select.component";
import { AccountDeskEventsStore } from "./account-desk-events-store.service";
import { AccountDeskAccountSwitcherComponent } from "./account-desk-account-switcher.component";
import { AccountDeskDirectoryStore } from "./account-desk-directory-store.service";
import { AccountDeskOperatorWorkspaceComponent } from "./account-desk-operator-workspace.component";
import { AccountDeskFleetStore } from "./account-desk-fleet-store.service";
import { AccountDeskGuidanceComponent } from "./account-desk-guidance.component";
import { AccountDeskGuidanceStore } from "./account-desk-guidance-store.service";
import { AccountDeskRecoveryStore } from "./account-desk-recovery-store.service";
import { AccountDeskSurfaceStore } from "./account-desk-surface-store.service";
import { AccountDeskTraderEventsComponent } from "./account-desk-trader-events.component";
import { AccountDeskTraderHoldingsComponent } from "./account-desk-trader-holdings.component";
import { AccountDeskVerdictComponent } from "./account-desk-verdict.component";
import { accountDeskFragmentTarget } from "./account-desk-legacy-fragments";

/** Account-id route host for the shared verdict spine and the later desk lenses. */
@Component({
  selector: "app-account-desk-page",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskAccountSwitcherComponent,
    AccountDeskGuidanceComponent,
    AccountDeskVerdictComponent,
    AccountDeskOperatorWorkspaceComponent,
    AccountDeskTraderHoldingsComponent,
    AccountDeskLensSelectComponent,
    AccountDeskTraderEventsComponent,
    ButtonModule,
    CardModule,
    MessageModule,
    PageHeaderComponent,
    PanelModule,
  ],
  templateUrl: "./account-desk-page.component.html",
  styleUrl: "./account-desk-page.component.scss",
})
export class AccountDeskPageComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly host = inject(ElementRef<HTMLElement>);
  private readonly injector = inject(Injector);
  readonly store = inject(AccountDeskSurfaceStore);
  readonly holdings = inject(AccountDeskHoldingsStore);
  readonly events = inject(AccountDeskEventsStore);
  readonly directory = inject(AccountDeskDirectoryStore);
  readonly guidance = inject(AccountDeskGuidanceStore);
  readonly fleet = inject(AccountDeskFleetStore);
  readonly recovery = inject(AccountDeskRecoveryStore);
  readonly lens = signal<AccountDeskLens>("trader");
  readonly lensOptions: AccountDeskLensOption[] = [
    { label: "Trader", value: "trader" },
    { label: "Operator", value: "operator" },
  ];
  private readonly nowMs = signal(Date.now());
  private readonly pendingFocusAnchor = signal<string | null>(null);

  readonly triage = this.store.triage;
  readonly loading = this.store.loading;
  readonly error = this.store.error;
  readonly showingStaleLastGood = this.store.showingStaleLastGood;
  readonly headlineMetrics = this.holdings.headlineMetrics;
  readonly displayAccountId = this.store.accountId;
  readonly pageTitle = computed(() => {
    const accountId = this.store.accountId();
    return accountId === null ? 'Account desk' : `Account desk · ${accountId}`;
  });
  readonly freshnessCountdown = computed(() => {
    const validUntilMs = this.triage()?.account_observation.valid_until_ms;
    return validUntilMs === null || validUntilMs === undefined
      ? null
      : fmtDurationRemaining(validUntilMs - this.nowMs());
  });

  constructor() {
    effect(() => {
      const anchor = this.pendingFocusAnchor();
      if (anchor === null || this.triage() === null) return;
      afterNextRender(
        {
          write: () => {
            const target = (
              this.host.nativeElement as HTMLElement
            ).querySelector<HTMLElement>(`#${anchor}`);
            if (target === null) return;
            target.focus();
            this.pendingFocusAnchor.set(null);
          },
        },
        { injector: this.injector },
      );
    });
    this.route.paramMap
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((params) => {
        const accountId = params.get("accountId");
        if (accountId !== null) {
          void this.store.load(accountId);
          void this.holdings.load(accountId);
          void this.events.load(accountId);
          void this.fleet.load(accountId);
          this.recovery.load(accountId);
          void this.directory.loadRoster();
          void this.directory.loadServiceStatus(accountId);
        }
      });
    this.route.fragment
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((fragment) => {
        const target = accountDeskFragmentTarget(fragment);
        if (target === null) return;
        this.selectLens(target.lens);
        this.pendingFocusAnchor.set(target.anchor);
        void this.router.navigate([], {
          relativeTo: this.route,
          fragment: undefined,
          queryParamsHandling: "preserve",
          replaceUrl: true,
        });
      });
    const intervalId = window.setInterval(
      () => this.nowMs.set(Date.now()),
      1_000,
    );
    this.destroyRef.onDestroy(() => window.clearInterval(intervalId));
  }

  selectLens(lens: AccountDeskLens): void {
    this.lens.set(lens);
  }

  retry(): void {
    this.store.retry();
  }

  switchAccount(accountId: string): void {
    if (accountId !== this.store.accountId()) {
      void this.router.navigate(["/broker/accounts", accountId]);
    }
  }

  followPrimaryMove(move: AccountTriageVerdictMove): void {
    void this.router.navigate([move.route], {
      fragment: move.fragment ?? undefined,
    });
  }
}
