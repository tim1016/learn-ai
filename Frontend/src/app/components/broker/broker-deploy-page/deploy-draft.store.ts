import { Injectable } from '@angular/core';

import type { DeployBotStrategy, DeploySizingOption } from '../v2-panel/lib/broker-v2-panel.service';

/**
 * The settings a Deploy form carries: what the owner picked in What and How.
 *
 * The bot's name is not here — the backend authors it (#2551) — and neither
 * is anything that proves a click: no review token, no typed Live phrase, no
 * evidence-only acknowledgement.
 */
export interface DeployTicketSettings {
  exitAllowanceBps: number | null;
  bandMultiple: number | null;
  spreadCapBps: number | null;
  strategyKey: DeployBotStrategy['strategy_key'] | '';
  symbol: string;
  sizingPreset: DeploySizingOption['preset'];
  quantity: number;
  // Spelled out, not `Extract<DeployExecutionMode['mode'], ...>`: the backend
  // enum is these four today, so `Extract` narrows nothing and quietly widens
  // with the wire. `null` until the owner chooses: no world is preselected
  // for them unless it is this lane's own Paper or Shadow (H17).
  executionMode: 'dry_run' | 'paper' | 'shadow' | 'live' | null;
  allowCarryover: boolean;
  parameters: Readonly<Record<string, unknown>>;
}

/** Which of the two foldable steps the owner has open for editing. */
export interface DeployStepEditing {
  readonly what: boolean;
  readonly how: boolean;
}

/**
 * One account's Deploy form, kept while the owner leaves it to fix a blocker
 * elsewhere and comes back (H9).
 *
 * `submissionKey` is the opaque idempotency input of the next Deploy
 * (#2551): minted when the draft is created, and kept with the settings it
 * was first sent with (`submittedContent`) so that a retry — a double click,
 * a lost response, a return from another page — asks for the same bot. While
 * `outcomeUnknown`, the key is kept whatever the settings become: only its
 * status read, or a definite answer, frees the form for a new key. The typed
 * dollar amount is the owner's own choice and is kept; its review and any
 * typed consent never are, so a restored amount is always re-previewed and
 * re-confirmed.
 */
export interface DeployDraft {
  readonly settings: DeployTicketSettings;
  readonly amount: string;
  readonly submissionKey: string;
  readonly submittedContent: string | null;
  /** A Deploy was sent under `submissionKey` and no answer settled it. */
  readonly outcomeUnknown: boolean;
  readonly editing: DeployStepEditing;
  /** Deploy again's display-only lineage: the bot this one follows. */
  readonly replaces: string | null;
}

export const EMPTY_DEPLOY_SETTINGS: DeployTicketSettings = {
  exitAllowanceBps: null,
  bandMultiple: null,
  spreadCapBps: null,
  strategyKey: '',
  symbol: '',
  sizingPreset: 'safe_canary',
  quantity: 1,
  executionMode: null,
  allowCarryover: false,
  parameters: {},
};

/** A fresh, never-submitted draft with its own submission key. */
export function freshDeployDraft(settings: DeployTicketSettings = EMPTY_DEPLOY_SETTINGS): DeployDraft {
  return {
    settings,
    amount: '',
    submissionKey: crypto.randomUUID(),
    submittedContent: null,
    outcomeUnknown: false,
    editing: { what: false, how: false },
    replaces: null,
  };
}

/**
 * JSON with object keys sorted at every depth, so two equal payloads always
 * spell the same string whatever order their keys were written in. The
 * submission key's content rule compares these: a key is reused only for
 * exactly the settings it was first sent with.
 */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, nested: unknown) =>
    nested !== null && typeof nested === 'object' && !Array.isArray(nested)
      ? Object.fromEntries(Object.entries(nested).sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0)))
      : nested,
  );
}

/**
 * The session's Deploy drafts, one per account, in memory only.
 *
 * Root-scoped so a draft outlives the Deploy page while the owner visits
 * Settings or a bot's page, and gone on a full reload — the recovery read
 * (`?submission=`) is what survives a reload, never the form.
 */
@Injectable({ providedIn: 'root' })
export class DeployDraftStore {
  private readonly drafts = new Map<string, DeployDraft>();

  read(account: string): DeployDraft | null {
    return this.drafts.get(account) ?? null;
  }

  write(account: string, draft: DeployDraft): void {
    this.drafts.set(account, draft);
  }
}
