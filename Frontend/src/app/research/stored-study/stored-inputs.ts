/**
 * Reads the inputs a stored run was asked, with the type each one must have.
 *
 * A record's inputs are JSON the data plane stored from a study's request, so a field is whatever the request held.
 * Each reader returns the field in its type or throws `StoredInputsError`, which the caller turns into words: a
 * rerun then says the record's inputs could not be read, and the page shows no fact it could not read, instead of a
 * default that was never entered.
 */
export class StoredInputsError extends Error {}

export class StoredInputs {
  constructor(private readonly raw: Readonly<Record<string, unknown>>) {}

  has(key: string): boolean {
    return this.raw[key] !== undefined && this.raw[key] !== null;
  }

  text(key: string): string {
    const value = this.raw[key];
    if (typeof value !== 'string') throw new StoredInputsError(`"${key}" is not text`);
    return value;
  }

  number(key: string): number {
    const value = this.raw[key];
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new StoredInputsError(`"${key}" is not a number`);
    return value;
  }

  boolean(key: string): boolean {
    const value = this.raw[key];
    if (typeof value !== 'boolean') throw new StoredInputsError(`"${key}" is not true or false`);
    return value;
  }

  texts(key: string): string[] {
    const value = this.raw[key];
    if (!Array.isArray(value) || !value.every((item): item is string => typeof item === 'string')) {
      throw new StoredInputsError(`"${key}" is not a list of text`);
    }
    return value;
  }

  numbers(key: string): number[] {
    const value = this.raw[key];
    if (!Array.isArray(value) || !value.every((item): item is number => typeof item === 'number')) {
      throw new StoredInputsError(`"${key}" is not a list of numbers`);
    }
    return value;
  }

  numberMap(key: string): Record<string, number> {
    const value = this.raw[key];
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new StoredInputsError(`"${key}" is not a set of named numbers`);
    }
    const entries = Object.entries(value);
    if (!entries.every((entry): entry is [string, number] => typeof entry[1] === 'number')) {
      throw new StoredInputsError(`"${key}" holds a value that is not a number`);
    }
    return Object.fromEntries(entries);
  }

  record(key: string): StoredInputs {
    const value = this.raw[key];
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new StoredInputsError(`"${key}" is not a set of fields`);
    }
    return new StoredInputs(value as Record<string, unknown>);
  }
}
