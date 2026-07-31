/**
 * Preserve JSON-imported structural shapes while widening literal values.
 *
 * TypeScript intentionally widens JSON strings and numbers, so committed
 * backend fixtures cannot directly satisfy generated closed unions. This
 * helper keeps their structure and nullability checked without re-authoring
 * backend semantics in TypeScript.
 */
export type JsonImported<T> = T extends string
  ? string
  : T extends number
    ? number
    : T extends boolean
      ? boolean
      : T extends readonly (infer Item)[]
        ? JsonImported<Item>[]
        : T extends object
          ? { [Key in keyof T]: JsonImported<T[Key]> }
          : T;
