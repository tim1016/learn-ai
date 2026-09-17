import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { DialogModule } from 'primeng/dialog';

import type { AlpacaDeskSelectionSummary } from '../../../api/alpaca.types';
import { LensPreferenceService } from '../../broker/shared/lens/lens-preference.service';
import { LensTabsComponent } from '../../broker/shared/lens/lens-tabs.component';
import { LENS_QUERY_PARAM, parseLens, type DeskLens } from '../../broker/shared/lens/lens';
import { lensNavigationExtras } from '../../broker/shared/lens/lens-url';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaDeskAccountStateComponent } from './alpaca-desk-account-state.component';
import { AlpacaOperatorLensComponent } from './alpaca-operator-lens.component';
import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';
import { AlpacaTraderLensComponent } from './alpaca-trader-lens.component';
import { AlpacaHoldBannerComponent } from './alpaca-hold-banner.component';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';
import { parseManualOrderTicketQuery } from '../../broker/lib/manual-order-navigation';
import {
  BrokersService,
  type SqliteTimelineQuery,
} from '../../../services/brokers.service';

function timelineQueryFromRoute(params: { get(name: string): string | null }): SqliteTimelineQuery | null {
  const rawSequence = params.get('timelineSequence');
  const sequence = rawSequence === null ? undefined : Number(rawSequence);
  const query: SqliteTimelineQuery = {
    strategyInstanceId: params.get('timelineBot') ?? undefined,
    orderRef: params.get('timelineOrderRef') ?? undefined,
    effectOperationId: params.get('timelineOperationRef') ?? undefined,
    uncertaintyId: params.get('timelineUncertaintyId') ?? undefined,
    executionId: params.get('timelineExecutionId') ?? undefined,
    transitionKind: params.get('timelineTransitionKind') ?? undefined,
    sequence:
      typeof sequence === 'number' && Number.isInteger(sequence) && sequence > 0
        ? sequence
        : undefined,
  };
  return Object.values(query).some((value) => value !== undefined) ? query : null;
}

/**
 * One account's Overview tab — the account workspace's empty child
 * (ADR 0064 Decision 1). It owns the persona choice (Trader / Operator) and
 * each lens owns its own data and content.
 *
 * The account header, the tabs, the Deploy action and the list of every
 * account are the workspace's, not this tab's: an operator sees each of them
 * once, above whichever tab is open, rather than once per page. The shared
 * account read comes from the workspace's `AlpacaDeskAccountDataService`,
 * which is why this component no longer provides one.
 */
@Component({
  selector: 'app-alpaca-desk',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaCustodyResolutionComponent,
    AlpacaDeskAccountStateComponent,
    AlpacaHoldBannerComponent,
    AlpacaOperatorLensComponent,
    AlpacaOrderEntryComponent,
    AlpacaTraderLensComponent,
    DialogModule,
    LensTabsComponent,
  ],
  templateUrl: './alpaca-desk.component.html',
  styleUrl: './alpaca-desk.component.scss',
  providers: [AlpacaOperatorLensDataService],
})
export class AlpacaDeskComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly operatorData = inject(AlpacaOperatorLensDataService);
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly brokers = inject(BrokersService);
  private readonly configuration = inject(BrokerConfigurationService);
  private readonly lensPreference = inject(LensPreferenceService);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  /** Null while the routed lane has not resolved in the directory; the
   * workspace header above says so, and this tab renders nothing. */
  protected readonly contextTarget = this.accountData.target;
  /** The frozen command fence order entry must mint against — never the live
   * directory (#2106). See `AlpacaDeskAccountDataService.fence`. */
  protected readonly contextFence = this.accountData.fence;

  private readonly configurationState = resource({
    params: () => this.contextTarget(),
    loader: ({ params }) =>
      params === null
        ? Promise.resolve(null)
        : this.configuration.readDeskState(params.clerkId),
  });
  protected readonly deskState = computed(() =>
    this.configurationState.hasValue() ? this.configurationState.value() : null,
  );

  protected readonly lens = linkedSignal<DeskLens>(() =>
    parseLens(this.queryParams().get(LENS_QUERY_PARAM)) ?? this.lensPreference.read() ?? 'trader',
  );
  protected readonly timelineQuery = computed(() => timelineQueryFromRoute(this.queryParams()));
  private readonly routedOrderPrefill = computed(() =>
    parseManualOrderTicketQuery(this.queryParams()),
  );
  protected readonly ticketAccountId = computed(() =>
    this.accountData.account.hasValue() ? this.accountData.account.value().account_id : null,
  );
  protected readonly accountSnapshot = computed(() =>
    this.accountData.account.hasValue() ? this.accountData.account.value() : null,
  );
  protected readonly operatingDeskVisible = computed(() =>
    this.contextTarget() !== null && this.accountData.account.hasValue(),
  );
  protected readonly accountFailed = computed(
    () => this.accountData.account.error() !== undefined,
  );
  protected readonly orderPrefill = computed(() => {
    const routed = this.routedOrderPrefill();
    return routed !== null && routed.accountId === this.ticketAccountId() ? routed : null;
  });
  protected readonly orderRouteMismatch = computed(() => {
    const routed = this.routedOrderPrefill();
    const accountId = this.ticketAccountId();
    return routed !== null && accountId !== null && routed.accountId !== accountId
      ? `The order link targets account ${routed.accountId}, but Alpaca is connected to ${accountId}. No ticket was opened.`
      : null;
  });
  private readonly manualOrderCapability = resource({
    params: () => {
      const accountId = this.orderPrefill()?.accountId;
      const target = this.contextTarget();
      return accountId === undefined || target === null ? undefined : { target, accountId };
    },
    loader: ({ params }) =>
      this.brokers.getSqliteManualOrderCapability(params.target.clerkId, params.accountId),
  });
  protected readonly manualTicketCapability = computed(
    () => this.manualOrderCapability.hasValue()
      ? this.manualOrderCapability.value()
      : null,
  );
  protected readonly manualOrderNotice = computed(() => {
    if (this.orderPrefill() === null) return null;
    if (this.manualOrderCapability.isLoading()) {
      return 'Checking the active order authority before opening this ticket.';
    }
    if (this.manualOrderCapability.error() !== undefined) {
      return 'The SQLite order authority is unavailable. No ticket was opened.';
    }
    return null;
  });
  protected readonly orderEntryOpen = signal(false);
  protected readonly historyRefreshVersion = signal(0);

  constructor() {
    effect(() => {
      if (this.operatingDeskVisible() && this.lens() === 'operator') this.operatorData.loadOnce();
    });
    effect(() => {
      this.orderEntryOpen.set(
        this.orderPrefill() !== null && this.manualOrderCapability.hasValue(),
      );
    });
  }

  protected selectLens(lens: DeskLens): void {
    if (lens === this.lens()) return;
    this.lens.set(lens);
    this.lensPreference.write(lens);
    void this.router.navigate([], {
      relativeTo: this.route,
      ...lensNavigationExtras(lens),
    });
  }

  protected refreshDesk(): void {
    this.historyRefreshVersion.update((version) => version + 1);
  }

  protected reviewAccount(choice: AlpacaDeskSelectionSummary | null): void {
    const target = this.contextTarget();
    if (target === null) return;
    const commands = ['/brokers', 'alpaca', 'clerks', target.clerkId, 'configuration'];
    if (choice === null) {
      void this.router.navigate(commands);
      return;
    }
    void this.router.navigate(commands, {
      queryParams: { profileId: choice.profile_id, revision: choice.revision },
    });
  }
}
