import { HttpErrorResponse } from '@angular/common/http';
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
import { TagModule } from 'primeng/tag';
import { DialogModule } from 'primeng/dialog';

import { AlpacaDeployDrawerComponent } from '../../broker/broker-deploy-page/alpaca-deploy-drawer.component';
import { AlpacaAccountCardComponent } from './alpaca-account-card.component';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaOperatorLensComponent } from './alpaca-operator-lens.component';
import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';
import { AlpacaTraderLensComponent } from './alpaca-trader-lens.component';
import { AlpacaHoldBannerComponent } from './alpaca-hold-banner.component';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';
import { parseManualOrderTicketQuery } from '../../broker/lib/manual-order-navigation';
import type { ManualOrderCapability } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';

const LENS_STORAGE_KEY = 'learn-ai.alpaca-desk.lens';

type AlpacaDeskLens = 'trader' | 'operator';
type ManualOrderAuthority =
  | { readonly kind: 'legacy' }
  | { readonly kind: 'sqlite'; readonly capability: ManualOrderCapability };

function lensFrom(value: string | null): AlpacaDeskLens | null {
  return value === 'trader' || value === 'operator' ? value : null;
}

function storedLens(): AlpacaDeskLens | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    return lensFrom(localStorage.getItem(LENS_STORAGE_KEY));
  } catch (error) {
    // Storage can be disabled without making the in-memory desk unusable.
    void error;
    return null;
  }
}

function persistLens(lens: AlpacaDeskLens): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(LENS_STORAGE_KEY, lens);
  } catch (error) {
    // Persistence is an enhancement; keep the current session's choice.
    void error;
  }
}

/**
 * Alpaca broker desk (Broker System v2) — the `/brokers/alpaca` route target.
 * The shell owns the persona choice; each lens owns its own data and content.
 */
@Component({
  selector: 'app-alpaca-desk',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaDeployDrawerComponent,
    AlpacaAccountCardComponent,
    AlpacaCustodyResolutionComponent,
    AlpacaHoldBannerComponent,
    AlpacaOperatorLensComponent,
    AlpacaOrderEntryComponent,
    AlpacaTraderLensComponent,
    DialogModule,
    TagModule,
  ],
  templateUrl: './alpaca-desk.component.html',
  styleUrl: './alpaca-desk.component.scss',
  providers: [AlpacaDeskAccountDataService, AlpacaOperatorLensDataService],
})
export class AlpacaDeskComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly operatorData = inject(AlpacaOperatorLensDataService);
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly brokers = inject(BrokersService);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  protected readonly lens = linkedSignal<AlpacaDeskLens>(() =>
    lensFrom(this.queryParams().get('lens')) ?? storedLens() ?? 'trader',
  );
  protected readonly deployOpen = linkedSignal(() => this.queryParams().has('deploy'));
  private readonly routedOrderPrefill = computed(() =>
    parseManualOrderTicketQuery(this.queryParams()),
  );
  protected readonly ticketAccountId = computed(() =>
    this.accountData.account.hasValue() ? this.accountData.account.value().account_id : null,
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
  private readonly manualOrderAuthority = resource({
    params: () => this.orderPrefill()?.accountId,
    loader: async ({ params }) => {
      try {
        return {
          kind: 'sqlite',
          capability: await this.brokers.getSqliteManualOrderCapability(params),
        } satisfies ManualOrderAuthority;
      } catch (error) {
        if (
          (error instanceof HttpErrorResponse && error.status === 409) ||
          (typeof error === 'object' && error !== null && 'status' in error && error.status === 409)
        ) {
          return { kind: 'legacy' } satisfies ManualOrderAuthority;
        }
        throw error;
      }
    },
  });
  protected readonly sqliteManualAuthority = computed(
    () => this.manualOrderAuthority.value()?.kind === 'sqlite',
  );
  protected readonly manualTicketCapability = computed(() => {
    const authority = this.manualOrderAuthority.value();
    return authority?.kind === 'sqlite' ? authority.capability : null;
  });
  protected readonly manualOrderNotice = computed(() => {
    if (this.orderPrefill() === null) return null;
    if (this.manualOrderAuthority.isLoading()) {
      return 'Checking the active order authority before opening this ticket.';
    }
    if (this.manualOrderAuthority.error() !== undefined) {
      return 'The active order authority could not be verified. No ticket was opened.';
    }
    return null;
  });
  protected readonly orderEntryOpen = signal(false);
  protected readonly historyRefreshVersion = signal(0);

  constructor() {
    effect(() => {
      if (this.lens() === 'operator') this.operatorData.loadOnce();
    });
    effect(() => {
      this.orderEntryOpen.set(
        this.orderPrefill() !== null && this.manualOrderAuthority.value() !== undefined,
      );
    });
  }

  protected selectLens(lens: AlpacaDeskLens): void {
    if (lens === this.lens()) return;
    this.lens.set(lens);
    persistLens(lens);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { lens },
      queryParamsHandling: 'merge',
    });
  }

  protected openDeploy(): void {
    this.deployOpen.set(true);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { deploy: '', deployLens: 'trader' },
      queryParamsHandling: 'merge',
    });
  }

  protected closeDeploy(): void {
    this.deployOpen.set(false);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { deploy: null, deployLens: null },
      queryParamsHandling: 'merge',
    });
  }

  protected refreshDesk(): void {
    this.historyRefreshVersion.update((version) => version + 1);
  }

  protected onLensKeydown(event: KeyboardEvent): void {
    const nextLens =
      event.key === 'ArrowRight' || event.key === 'End'
        ? 'operator'
        : event.key === 'ArrowLeft' || event.key === 'Home'
          ? 'trader'
          : null;
    if (nextLens === null) return;
    event.preventDefault();
    this.selectLens(nextLens);
    if (!(event.currentTarget instanceof HTMLElement)) return;
    const target = event.currentTarget.parentElement?.querySelector(`[data-lens="${nextLens}"]`);
    if (target instanceof HTMLElement) target.focus();
  }
}
