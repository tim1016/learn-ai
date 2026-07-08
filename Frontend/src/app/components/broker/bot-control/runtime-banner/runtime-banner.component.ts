import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import type {
  OperatorNotice,
  OperatorSurfaceRuntimeFreshness,
} from '../../../../api/live-instances.types';
import { OperatorNoticeComponent } from '../../../operator-notice/operator-notice.component';

/** Hold non-critical freshness headlines back until the stale state has
 *  persisted for this many milliseconds. Bot Control polls /status every 4s,
 *  so bar arrival being briefly late and then catching up would otherwise
 *  flip the banner on and off across consecutive polls. Three poll cycles
 *  (12s) is the floor at which the staleness stops being transient. Critical
 *  tier notices bypass this gate per ADR-0013 §3 and render immediately. */
export const STALE_DEBOUNCE_MS = 12_000;
/** Keep a critical freshness headline visible through one normal status-poll
 *  recovery blip. Critical notices still render immediately; this only delays
 *  clearing them long enough to avoid a page-wide flash when the backend sees
 *  one fresh sample between stale command-loop samples. */
export const CRITICAL_CLEAR_DEBOUNCE_MS = 5_000;

@Component({
  selector: 'app-runtime-banner',
  templateUrl: './runtime-banner.component.html',
  styleUrl: './runtime-banner.component.scss',
  imports: [OperatorNoticeComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RuntimeBannerComponent {
  readonly freshness = input.required<OperatorSurfaceRuntimeFreshness | null>();
  /** PR 2 / PR 5 — post-halt watchdog incident headline. When non-null, rendered
   *  above the freshness headline. Critical-tier incidents are always shown above
   *  lower-priority notices per ADR-0013 §3. */
  readonly incidentHeadline = input<OperatorNotice | null>(null);
  readonly actionClicked = output<OperatorNotice>();

  private readonly _destroyRef = inject(DestroyRef);
  // Re-evaluated on every clock tick so the debounce window can release
  // without waiting for the next /status response to arrive.
  private readonly _now = signal<number>(Date.now());
  // Wall-clock at which the current stale episode began. Null when the
  // freshness headline is clear; set when it first becomes non-null and
  // kept stable across subsequent polls until it clears again.
  private readonly _firstStaleAt = signal<number | null>(null);
  private readonly _lastCriticalHeadline = signal<OperatorNotice | null>(null);
  private readonly _lastCriticalSeenAt = signal<number | null>(null);

  constructor() {
    const handle = setInterval(() => this._now.set(Date.now()), 1_000);
    this._destroyRef.onDestroy(() => clearInterval(handle));

    effect(() => {
      const headline = this.freshness()?.headline ?? null;
      untracked(() => {
        if (headline === null) {
          this._firstStaleAt.set(null);
        } else if (this._firstStaleAt() === null) {
          this._firstStaleAt.set(Date.now());
        }
      });
    });
    effect(() => {
      const headline = this.freshness()?.headline ?? null;
      untracked(() => {
        if (headline?.tier === 'critical') {
          this._lastCriticalHeadline.set(headline);
          this._lastCriticalSeenAt.set(Date.now());
        }
      });
    });
  }

  readonly headline = computed<OperatorNotice | null>(() => {
    const candidate = this.freshness()?.headline ?? null;
    if (candidate !== null && candidate.tier === 'critical') return candidate;
    const recentCritical = this.recentCriticalHeadline();
    if (recentCritical !== null) return recentCritical;
    if (candidate === null) return null;
    const firstAt = this._firstStaleAt();
    if (firstAt === null) return null;
    return this._now() - firstAt >= STALE_DEBOUNCE_MS ? candidate : null;
  });

  private readonly recentCriticalHeadline = computed<OperatorNotice | null>(() => {
    const headline = this._lastCriticalHeadline();
    const seenAt = this._lastCriticalSeenAt();
    if (headline === null || seenAt === null) return null;
    return this._now() - seenAt < CRITICAL_CLEAR_DEBOUNCE_MS ? headline : null;
  });

  readonly additionalReasons = computed<OperatorNotice[]>(() => {
    const visibleHeadline = this.headline();
    return visibleHeadline !== null && visibleHeadline === this.freshness()?.headline
      ? this.freshness()?.additional_reasons ?? []
      : [];
  });

  /** True when either the incident headline or the freshness headline is visible. */
  readonly hasBannerContent = computed<boolean>(
    () => this.incidentHeadline() !== null || this.headline() !== null,
  );

  forwardAction(notice: OperatorNotice): void {
    this.actionClicked.emit(notice);
  }
}
