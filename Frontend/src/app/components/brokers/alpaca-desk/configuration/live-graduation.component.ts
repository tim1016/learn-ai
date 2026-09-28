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

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { resourceTarget, type ResourceTarget } from '../../../../fleet/resource-target';
import { LiveGraduationReviewComponent } from './live-graduation-review.component';
import {
  LiveGraduationService,
  type LiveGraduationPlan,
} from './live-graduation.service';

type CeremonyPhase = 'idle' | 'planning' | 'review' | 'activating' | 'applying' | 'restarting';
type StageProgress = 'done' | 'current' | 'todo';

/** A stage's progress in words, so the rail never states it by colour alone. */
const PROGRESS_LABEL: Readonly<Record<Exclude<StageProgress, 'todo'>, string>> = {
  done: 'done',
  current: 'now',
};

function refusalMessage(error: unknown): string {
  if (!(error instanceof HttpErrorResponse)) {
    return 'The account authority action did not complete. Refresh its status before retrying.';
  }
  const detail = error.error?.detail;
  const message = typeof detail?.message === 'string' ? detail.message : error.message;
  const next = typeof detail?.next_action === 'string' ? detail.next_action : null;
  return next === null ? message : `${message} ${next}`;
}

@Component({
  selector: 'app-live-graduation',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [LiveGraduationReviewComponent],
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
  private readonly restartTarget = signal<'shadow' | 'live'>('live');
  private pollHandle: ReturnType<typeof setInterval> | null = null;

  protected readonly status = resource({
    params: () => ({ clerkId: this.clerkId(), accountId: this.accountId() }),
    loader: ({ params }) => this.service.readStatus(params.clerkId, params.accountId),
  });
  protected readonly PROGRESS_LABEL = PROGRESS_LABEL;
  /** Shadow → Review → Live, each stage's progress read from the backend's state. */
  protected readonly stages = computed(() => {
    const status = this.status.hasValue() ? this.status.value() : null;
    const graduated = status?.state === 'graduated';
    const progress = (done: boolean, current = false): StageProgress =>
      done ? 'done' : current ? 'current' : 'todo';
    return [
      { key: 'shadow', number: 1, name: 'Shadow', note: 'simulated fills',
        progress: progress(status !== null && status.authority !== 'unavailable') },
      { key: 'review', number: 2, name: 'Review', note: 'evidence checked',
        progress: progress(graduated, status?.state === 'review_available') },
      { key: 'live', number: 3, name: 'Live', note: 'real orders', progress: progress(graduated) },
    ] as const;
  });
  protected readonly busy = computed(() =>
    ['planning', 'activating', 'applying', 'restarting'].includes(this.phase()),
  );
  protected reviewExpired(): boolean {
    const plan = this.plan();
    return plan !== null && Date.now() >= plan.expires_at_ms;
  }

  constructor() {
    // Route reuse keeps this component instance across an account switch —
    // its own inputs just update. Without this, a stale plan and an already
    // -ticked acknowledgement could stay on screen labeled for the new
    // account, which is exactly what a real-money confirmation must never do.
    effect(() => {
      this.clerkId();
      this.accountId();
      this.resetCeremony();
    });
    effect(() => {
      if (this.status.hasValue() && (this.status.value().state === 'graduated' ||
        (this.restartTarget() === 'shadow' && this.phase() === 'restarting' && this.status.value().state === 'review_available'))) {
        this.resetCeremony();
      }
    });
    this.destroyRef.onDestroy(() => this.stopPolling());
  }

  private resetCeremony(): void {
    this.phase.set('idle');
    this.plan.set(null);
    this.acknowledged.set(false);
    this.refusal.set(null);
    this.stopPolling();
  }

  protected activateShadow(): void {
    if (this.busy() || !this.status.hasValue() || this.status.value().state !== 'activation_available') return;
    const target = this.commandTarget();
    this.phase.set('activating');
    this.refusal.set(null);
    this.restartTarget.set('shadow');
    void this.service.activateShadow(target).then(
      () => {
        if (!this.isCurrent(target)) return;
        this.phase.set('restarting');
        this.beginPolling();
      },
      (error: unknown) => {
        if (!this.isCurrent(target)) return;
        this.refusal.set(refusalMessage(error));
        this.phase.set('idle');
        this.status.reload();
      },
    );
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
    if (plan === null || !this.acknowledged() || this.busy()) return;
    if (this.reviewExpired()) {
      // reviewExpired() reads Date.now(), not a signal, so a zoneless OnPush
      // pass may not have re-rendered the disabled/expired state yet if time
      // passed with no other interaction — surface the refusal rather than
      // silently dropping this click.
      this.refusal.set('This review expired. Refresh evidence before graduating.');
      return;
    }
    const target = this.commandTarget();
    this.restartTarget.set('live');
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
