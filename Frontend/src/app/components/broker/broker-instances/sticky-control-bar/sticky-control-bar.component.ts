import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import type {
  LiveInstanceStatus,
  ReadinessVerdict,
} from '../../../../api/live-instances.types';

type StatePillKind = 'running' | 'stopping' | 'stopped' | 'idle' | 'unreachable';
type ReadinessPillKind = 'ready' | 'blocked' | 'degraded' | 'unknown' | 'no_readiness';

interface StatePill {
  label: string;
  kind: StatePillKind;
}

interface ReadinessPill {
  label: string;
  kind: ReadinessPillKind;
}

/**
 * Sticky control bar — the persistent per-bot identity + status strip that
 * stays visible while the operator scrolls the long control panel.
 *
 * Issue #565 PR 12 — MVP scope.
 *
 * User stories covered in this MVP:
 * - #1, #2 sticky positioning so the bot's identity + status never leave
 *   the viewport while the trader is reading any of the down-page cards
 * - #3 persistent PAPER pill so paper mode is never confused with live
 * - #51 lead with the most-urgent fact (readiness verdict pill goes first)
 *
 * User stories deferred to a follow-up so that Start / Pause / Stop logic
 * does not move underneath an in-flight refactor:
 * - #31-#40 destructive kebab menu, conditional Restart & Update, paper
 *   reset — kebab affordance ships with this PR as a "scroll to advanced
 *   actions" link only, so destructive logic stays where it has been
 *   end-to-end tested. Real kebab dialogs land in a follow-up that owns
 *   only the dialog wiring.
 *
 * Per the issue body: "Safety-critical controls land LAST so the existing
 * parent component remains the source of truth for Start/Pause/Stop until
 * the surrounding context is stable." This MVP honours that by NOT
 * duplicating the Start / Pause / Stop buttons in the sticky bar — a
 * "Jump to controls" button scrolls the existing Start/Stop card into
 * view instead. Full control extraction lands in PR13 cleanup.
 */
@Component({
  selector: 'app-sticky-control-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './sticky-control-bar.component.html',
  styleUrl: './sticky-control-bar.component.scss',
})
export class StickyControlBarComponent {
  readonly status = input.required<LiveInstanceStatus>();
  /** Paper mode is conveyed by the account id (DU... prefix on IBKR) and
   * surfaced by the fleet header. The sticky bar receives it as an input
   * so it doesn't re-derive paper-vs-live from heuristics. */
  readonly isPaper = input.required<boolean>();

  readonly statePill = computed<StatePill>(() => {
    const state = this.status().process.state;
    switch (state) {
      case 'running':
        return { label: 'RUNNING', kind: 'running' };
      case 'stopping':
        return { label: 'STOPPING', kind: 'stopping' };
      case 'exited':
      case 'idle':
        return { label: 'STOPPED', kind: 'stopped' };
      case 'unreachable':
        return { label: 'UNREACHABLE', kind: 'unreachable' };
      default:
        return { label: 'UNKNOWN', kind: 'idle' };
    }
  });

  readonly readinessPill = computed<ReadinessPill>(() => {
    const verdict: ReadinessVerdict | undefined = this.status().readiness?.verdict;
    if (!verdict) return { label: 'NO READINESS', kind: 'no_readiness' };
    switch (verdict) {
      case 'READY':
        return { label: 'READY', kind: 'ready' };
      case 'BLOCKED':
        return { label: 'BLOCKED', kind: 'blocked' };
      case 'DEGRADED':
        return { label: 'DEGRADED', kind: 'degraded' };
      default:
        return { label: 'UNKNOWN', kind: 'unknown' };
    }
  });

  readonly botName = computed<string>(() => this.status().strategy_instance_id);

  readonly hasPoisonFlag = computed<boolean>(() => {
    const trigger = this.status().last_exit?.halt_trigger;
    return trigger !== null && trigger !== undefined;
  });

  /** Emitted when the operator clicks "Jump to controls". The parent
   * scrolls the existing Start/Stop card into view; the sticky bar does
   * not own the controls themselves. */
  readonly jumpToControlsRequested = output();

  onJumpToControlsClick(): void {
    this.jumpToControlsRequested.emit(undefined);
  }
}
