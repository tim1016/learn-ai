// `@types/node` is not a declared dependency of this app (only pulled in
// transitively by the build toolchain), so it is not something app code may
// rely on. `lane-fence-freeze.contract.spec.ts` is a structural spec that
// walks the source tree on disk -- Node's `fs`/`path` builtins, not browser
// APIs -- so it needs just enough of a type surface to compile. Kept
// minimal and scoped to exactly what that spec uses.
declare module 'node:fs' {
  export interface Dirent {
    readonly name: string;
    isDirectory(): boolean;
  }
  export function readFileSync(path: string, encoding: 'utf8'): string;
  export function readdirSync(path: string, options: { withFileTypes: true }): Dirent[];
}

declare module 'node:path' {
  export function join(...paths: string[]): string;
  export const sep: string;
}

declare const __dirname: string;
