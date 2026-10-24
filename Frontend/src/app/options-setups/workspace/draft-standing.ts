import type { DraftStanding } from './options-setup-store';

export interface StandingWords {
  /** The short fact, for a chip beside the setup's name. */
  readonly label: string;
  /** What it means for Save, said where the draft is edited. */
  readonly detail: string;
}

/**
 * Whether the draft still matches the content last saved under its name, in
 * words. A name is a label on a draft that can be edited, so the draft says
 * both: what it was opened or saved as, and whether it has changed since.
 */
export function describeStanding(standing: DraftStanding): StandingWords {
  switch (standing.kind) {
    case 'unsaved':
      return { label: 'Not saved', detail: 'This draft has no saved name yet. Name it and save it to keep it.' };
    case 'matches':
      return {
        label: standing.example ? 'Shipped example' : 'Matches saved',
        detail: standing.example ? `This draft is the shipped example “${standing.name}”.` : `This draft matches “${standing.name}” as last saved.`,
      };
    case 'changed':
      return {
        label: 'Changed since saved',
        detail: standing.example
          ? `This draft has changed from the shipped example “${standing.name}”, which cannot be overwritten. Give it a name of its own to save it.`
          : `This draft has changed since it was saved as “${standing.name}”. Save under that name to replace it, or under another name to add a new entry.`,
      };
  }
}
