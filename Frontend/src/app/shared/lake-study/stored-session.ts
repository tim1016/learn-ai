import { StoredInputsError, type StoredInputs } from '../../research/stored-study/stored-inputs';
import type { Session } from '../ticker-range-picker/ticker-range-picker.types';

/**
 * The session a saved lake-study run computed on: the one it names, or `fallback` when it names none, which is the
 * session that study computed on before its request named one. A name that is not a session this page offers is
 * unreadable, never a default that was not entered.
 */
export function storedSession(stored: StoredInputs, fallback: Session): Session {
  if (!stored.has('session')) return fallback;
  const session = stored.text('session');
  if (session !== 'rth' && session !== 'extended') {
    throw new StoredInputsError(`"${session}" is not a session this page offers`);
  }
  return session;
}
