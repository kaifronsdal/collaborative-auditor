/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Empty stub for optional heavy deps pulled in transitively via
 * `@tsmono/util`'s barrel (`arrow.ts` → flechette/arquero/lz4js; `json-worker`
 * → json5). We never call those helpers; this keeps the dev-server module
 * graph resolvable without adding ~2 MB of unused deps. Named exports
 * mirror what `arrow.ts`/`json-worker.ts` import so esbuild's dep-scan
 * doesn't warn. See vite.config.ts `resolve.alias`.
 */
export const CompressionType: any = undefined;
export const setCompressionCodec: any = undefined;
export const tableFromIPC: any = undefined;
export const table: any = undefined;
export const escape: any = undefined;
export const fromArrow: any = undefined;
export const decompress: any = undefined;
export const parse: any = undefined;
export default {} as any;
