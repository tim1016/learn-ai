import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { CurrencyPipe, DatePipe, PercentPipe } from '@angular/common';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { resourceTarget, type ResourceTarget } from '../../../../fleet/resource-target';
import {
  LiveGraduationService,
  type LiveGraduationPlan,
} from './live-graduation.service';

type CeremonyPhase = 'idle' | 'planning' | 'review' | 'applying' | 'restarting';

function refusalMessage(error: unknown): string {
  if (!(error instanceof HttpErrorResponse)) {
    return 'The graduation ceremony did not complete. Nothing was forced.';
  }
  const detail = error.error?.detail;
  const message = typeof detail?.message === 'string' ? detail.message : error.message;
  const next = typeof detail?.next_action === 'string' ? detail.next_action : null;
  return next === null ? message : `${message} ${next}`;
}

@Component({
  selector: 'app-live-graduation',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DatePipe, PercentPipe],
  templateUrl: './live-graduation.component.html',
  styleUrl: './live-graduation.component.scss',
})
export class LiveGraduationComponent {
  private readonly service = inject(LiveGraduationService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly destroyRef = inject(DestroyRef);

  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();

  protected readonly phase = signal<CeremonyPhase>('idle');
  protected readonly plan = signal<LiveGraduationPlan | null>(null);
  protected readonly acknowledged = signal(false);
  protected readonly refusal = signal<string | null>(null);
  private pollHandle: ReturnType<typeof setInterval> | null = null;

  protected readonly status = resource({
    params: () => ({ clerkId: this.clerkId(), accountId: this.accountId() }),
    loader: ({ params }) => this.service.readStatus(params.clerkId, params.accountId),
  });
  protected readonly busy = computed(() =>
    ['planning', 'applying', 'restarting'].includes(this.phase()),
  );
  protected reviewExpired(): boolean {
    const plan = this.plan();
    return plan !== null && Date.now() >= plan.expires_at_ms;
  }

  constructor() {
    effect(() => {
      if (this.status.hasValue() && this.status.value().state === 'graduated') {
        this.phase.set('idle');
        this.plan.set(null);
        this.acknowledged.set(false);
        this.stopPolling();
      }
    });
    this.destroyRef.onDestroy(() => this.stopPolling());
  }

  protected prepareReview(): void {
    if (this.busy()) return;
    const target = this.commandTarget();
    this.phase.set('planning');
    this.refusal.set(null);
    this.plan.set(null);
    this.acknowledged.set(false);
    void this.service.prepare(target).then(
      (plan) => {
        if (!this.isCurrent(target)) return;
        this.plan.set(plan);
        this.phase.set('review');
      },
      (error: unknown) => {
        if (!this.isCurrent(target)) return;
        this.refusal.set(refusalMessage(error));
        this.phase.set('idle');
        this.status.reload();
      },
    );
  }

  protected confirm(): void {
    const plan = this.plan();
    if (plan === null || !this.acknowledged() || this.reviewExpired() || this.busy()) return;
    const target = this.commandTarget();
    this.phase.set('applying');
    this.refusal.set(null);
    void this.service.apply(target, plan).then(
      () => {
        if (!this.isCurrent(target)) return;
        this.phase.set('restarting');
        this.beginPolling();
      },
      (error: unknown) => {
        if (!this.isCurrent(target)) return;
        this.refusal.set(refusalMessage(error));
        this.phase.set('review');
      },
    );
  }

  protected refresh(): void {
    this.refusal.set(null);
    this.status.reload();
  }

  private beginPolling(): void {
    this.stopPolling();
    this.pollHandle = setInterval(() => this.status.reload(), 2_000);
    this.status.reload();
  }

  private stopPolling(): void {
    if (this.pollHandle === null) return;
    clearInterval(this.pollHandle);
    this.pollHandle = null;
  }

  private commandTarget(): ResourceTarget {
    const clerkId = this.clerkId();
    const accountId = this.accountId();
    const lane = this.fleetDirectory.lane('alpaca', clerkId);
    if (typeof globalThis.crypto?.randomUUID !== 'function') {
      throw new Error('This browser cannot create a durable request identity.');
    }
    return resourceTarget('alpaca', clerkId, {
      accountId,
      capability: 'custody_command',
      idempotencyKey: globalThis.crypto.randomUUID(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
  }

  private isCurrent(target: ResourceTarget): boolean {
    return target.clerkId === this.clerkId() && target.accountId === this.accountId();
  }
}
