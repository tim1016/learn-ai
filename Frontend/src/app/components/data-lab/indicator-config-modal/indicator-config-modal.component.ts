import { ChangeDetectionStrategy, Component, OnDestroy, computed, effect, input, model, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DialogModule } from 'primeng/dialog';
import { Tooltip } from 'primeng/tooltip';
import { KatexDirective } from '../../../shared/katex.directive';
import {
  CATEGORY_META,
  getIndicatorReference,
  IndicatorReferenceEntry,
} from '../../../shared/indicators/indicator-reference';
import {
  ActiveIndicatorEntry,
  ActiveIndicatorParam,
} from '../active-indicator-card/active-indicator-card.component';

@Component({
  selector: 'app-indicator-config-modal',
  standalone: true,
  imports: [CommonModule, FormsModule, DialogModule, Tooltip, KatexDirective],
  templateUrl: './indicator-config-modal.component.html',
  styleUrls: ['./indicator-config-modal.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class IndicatorConfigModalComponent implements OnDestroy {
  visible = model<boolean>(false);
  /** 'configure' (default): bound to an active entry; param edits flow up to
   *  the parent. 'preview': a what-if for an indicator the user is browsing
   *  in the catalog — local param state, no link to active entries until
   *  the user hits "Add to active". */
  mode = input<'configure' | 'preview'>('configure');
  entry = input<ActiveIndicatorEntry | null>(null);
  paramConfigs = input<ActiveIndicatorParam[]>([]);
  /** Names of indicators currently in the active list (with any params). */
  activeIndicatorKeys = input<readonly string[]>([]);

  paramChange = output<{ name: string; value: number }>();
  resetDefaults = output();
  /** Reset just one parameter to its INDICATOR_CONFIGS default. */
  resetParam = output<string>();
  addRelated = output<string>();
  /** Remove the last active entry with the given indicator key. */
  removeRelated = output<string>();
  /** Preview mode: user clicked "Add to active" with the current param set. */
  addPreview = output<{ key: string; params: Record<string, number> }>();

  // ── Pin/inspector mode ──────────────────────────────────────
  /** When true, the dialog renders as a right-anchored 360px inspector
   *  instead of a centered modal. Persisted across sessions. */
  protected pinned = signal<boolean>(this.readPinnedFromStorage());

  private static readonly PIN_STORAGE_KEY = 'data-lab.indicator-config.mode';

  constructor() {
    effect(() => {
      const p = this.pinned();
      try {
        localStorage.setItem(IndicatorConfigModalComponent.PIN_STORAGE_KEY, p ? 'pinned' : 'modal');
      } catch {
        // localStorage may be unavailable (private mode, SSR) — silently skip persist.
      }
      // The Data Lab IDE shell now owns the right rail natively, so the
      // --inspector-w gutter that pinned mode used to write is no longer
      // read by anything. Pinned-mode rendering will be retired in Phase 5
      // once the rail-native inspector lands; for now it falls back to the
      // centered/overlay mode.
    });

    // Seed preview-mode params from INDICATOR_CONFIGS defaults whenever
    // the modal opens with a new entry in preview mode.
    effect(() => {
      if (this.mode() !== 'preview') return;
      const e = this.entry();
      if (!e || !this.visible()) return;
      this.previewParams.set(this.defaultParamMap());
    });
  }

  protected togglePin(): void {
    this.pinned.update((p) => !p);
  }

  private readPinnedFromStorage(): boolean {
    try {
      return localStorage.getItem(IndicatorConfigModalComponent.PIN_STORAGE_KEY) === 'pinned';
    } catch {
      return false;
    }
  }

  // ── Preview-mode local param state ─────────────────────────
  /** Local param values used while the modal is in preview mode. Seeded
   *  from `paramConfigs` defaults whenever the modal opens or the entry
   *  key changes. Effects below keep this in sync. */
  private previewParams = signal<Record<string, number>>({});

  /** The "effective" entry the modal renders. In configure mode this is
   *  the active entry passed from the parent; in preview mode it's a
   *  synthetic record built from the entry key + local previewParams. */
  protected effectiveEntry = computed<ActiveIndicatorEntry | null>(() => {
    if (this.mode() === 'preview') {
      const e = this.entry();
      if (!e) return null;
      return { name: e.name, params: this.previewParams() };
    }
    return this.entry();
  });

  protected reference = computed<IndicatorReferenceEntry | null>(() => {
    const e = this.effectiveEntry();
    return e ? getIndicatorReference(e.name) : null;
  });

  protected categoryLabel = computed(() => {
    const r = this.reference();
    return r ? CATEGORY_META[r.category].label : '';
  });

  protected categoryColor = computed(() => {
    const r = this.reference();
    return r ? CATEGORY_META[r.category].color : 'transparent';
  });

  protected panelLabel = computed(() => {
    const r = this.reference();
    return r ? (r.panelType === 'overlay' ? 'Overlay' : 'Sub-panel') : '';
  });

  /** True for params whose current value differs from INDICATOR_CONFIGS default. */
  protected modifiedParams = computed<Set<string>>(() => {
    const e = this.effectiveEntry();
    if (!e) return new Set();
    const out = new Set<string>();
    for (const p of this.paramConfigs()) {
      const v = e.params[p.name];
      if (typeof v === 'number' && v !== p.default) out.add(p.name);
    }
    return out;
  });

  protected modifiedCount = computed(() => this.modifiedParams().size);
  protected hasModified = computed(() => this.modifiedCount() > 0);

  protected isParamModified(name: string): boolean {
    return this.modifiedParams().has(name);
  }

  protected resetButtonLabel = computed(() => {
    const n = this.modifiedCount();
    if (n === 0) return 'Reset to defaults';
    return n === 1 ? 'Reset to defaults (1 change)' : `Reset to defaults (${n} changes)`;
  });

  protected onParamChange(name: string, value: number | string): void {
    const num = typeof value === 'string' ? parseFloat(value) : value;
    if (!Number.isFinite(num)) return;
    if (this.mode() === 'preview') {
      this.previewParams.update((p) => ({ ...p, [name]: num }));
    } else {
      this.paramChange.emit({ name, value: num });
    }
  }

  protected onReset(): void {
    if (this.mode() === 'preview') {
      this.previewParams.set(this.defaultParamMap());
    } else {
      this.resetDefaults.emit();
    }
  }

  protected onResetParam(name: string, ev: MouseEvent): void {
    ev.stopPropagation();
    if (this.mode() === 'preview') {
      const def = this.paramConfigs().find((p) => p.name === name)?.default;
      if (typeof def === 'number') {
        this.previewParams.update((p) => ({ ...p, [name]: def }));
      }
    } else {
      this.resetParam.emit(name);
    }
  }

  /** Used in preview mode: snapshot the current preview params and emit
   *  back to the parent so it can call addInstance with the user's choices. */
  protected onAddPreview(): void {
    const e = this.entry();
    if (!e) return;
    this.addPreview.emit({ key: e.name, params: { ...this.previewParams() } });
    this.visible.set(false);
  }

  /** Initial param map for preview mode — defaults from INDICATOR_CONFIGS. */
  private defaultParamMap(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const p of this.paramConfigs()) out[p.name] = p.default;
    return out;
  }

  // ── Related-indicator chip state ────────────────────────────
  /** Indicator keys currently in their 3-second "just added · undo" window. */
  protected pendingUndo = signal<Set<string>>(new Set());
  private undoTimeouts = new Map<string, ReturnType<typeof setTimeout>>();

  /** Three states per related chip: idle (+ Add), pending-undo (✓ Added · undo),
   *  already-active (× Remove). */
  protected relatedChipState(name: string): 'idle' | 'pending' | 'active' {
    if (this.pendingUndo().has(name)) return 'pending';
    if (this.activeIndicatorKeys().includes(name)) return 'active';
    return 'idle';
  }

  protected onRelatedClick(name: string): void {
    const state = this.relatedChipState(name);
    if (state === 'pending') {
      // Click during the 3s window — undo.
      this.removeRelated.emit(name);
      this.clearUndoFor(name);
      return;
    }
    if (state === 'active') {
      // Already in active list — remove the last matching entry.
      this.removeRelated.emit(name);
      return;
    }
    // Idle — add and start the 3s undo window.
    this.addRelated.emit(name);
    this.pendingUndo.update((s) => {
      const next = new Set(s);
      next.add(name);
      return next;
    });
    const timeoutId = setTimeout(() => this.clearUndoFor(name), 3000);
    this.undoTimeouts.set(name, timeoutId);
  }

  private clearUndoFor(name: string): void {
    const t = this.undoTimeouts.get(name);
    if (t) {
      clearTimeout(t);
      this.undoTimeouts.delete(name);
    }
    this.pendingUndo.update((s) => {
      if (!s.has(name)) return s;
      const next = new Set(s);
      next.delete(name);
      return next;
    });
  }

  protected close(): void {
    this.visible.set(false);
  }

  ngOnDestroy(): void {
    for (const t of this.undoTimeouts.values()) clearTimeout(t);
    this.undoTimeouts.clear();
  }
}
