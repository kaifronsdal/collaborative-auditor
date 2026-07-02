/// <reference types="vitest/config" />
import { realpathSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const stub = new URL("./src/lib/empty-stub.ts", import.meta.url).pathname;
// `@tsmono/*` are pnpm-linked from inspect_ai's ts-mono workspace. That
// workspace isn't installed, so intra-@tsmono imports (e.g. inspect-components
// → @tsmono/util) fail node resolution from the linked package's real path.
// Alias them explicitly so vite resolves them regardless of importer location.
// Resolve the real ts-mono/packages/ root once via the linked util symlink so
// sibling packages we don't link directly (theme) are also reachable.
const tsmonoPkgs = realpathSync(
  new URL("./node_modules/@tsmono/util", import.meta.url).pathname
).replace(/\/util$/, "");
const tsmono = (p: string): string => `${tsmonoPkgs}/${p}`;

export default defineConfig({
  plugins: [react()],
  // Linked `@tsmono/*` packages ship a `tsconfig.json` that
  // `extends: "@tsmono/tsconfig/react.json"`, which only resolves inside
  // inspect_ai's own pnpm workspace. Vite's esbuild transform walks up from
  // each source file to find its tsconfig, hits that `extends`, and 500s.
  // Pin an inline tsconfig so esbuild never walks — it only strips types
  // (JSX is handled by plugin-react), so this is all it needs. Must be a
  // *string* — an object still triggers vite's tsconfck lookup (config.js
  // `transformWithEsbuild`: `typeof tsconfigRaw !== "string"`).
  esbuild: {
    tsconfigRaw: JSON.stringify({
      compilerOptions: { jsx: "react-jsx", useDefineForClassFields: true },
    }),
  },
  resolve: {
    // `@tsmono/util`'s barrel re-exports `arrow.ts` which pulls heavy deps
    // (flechette/arquero/lz4js) that inspect-view has but we don't. We only
    // reach the barrel transitively via `@tsmono/inspect-components/chat`
    // for `isJson`/`decodeHtmlEntities`; stub the unused optional deps so
    // the dev server doesn't 500 on the barrel import.
    alias: [
      { find: "@uwdata/flechette", replacement: stub },
      { find: "arquero", replacement: stub },
      { find: "lz4js", replacement: stub },
      { find: "json5", replacement: stub },
      // exact bare-specifier aliases (checked before the /src/ prefix rules)
      { find: /^@tsmono\/util$/, replacement: tsmono("util/src/index.ts") },
      { find: /^@tsmono\/inspect-common$/, replacement: tsmono("inspect-common/src/types/index.ts") },
      { find: /^@tsmono\/inspect-common\/types$/, replacement: tsmono("inspect-common/src/types/index.ts") },
      { find: /^@tsmono\/inspect-common\/utils$/, replacement: tsmono("inspect-common/src/utils/index.ts") },
      { find: /^@tsmono\/theme\/bootstrap$/, replacement: tsmono("theme/src/bootstrap.ts") },
      // subpath → src/<subpath> (react/hooks, react/state, inspect-components/chat, …)
      { find: /^@tsmono\/react$/, replacement: tsmono("react/src/index.ts") },
      { find: /^@tsmono\/react\//, replacement: tsmono("react/src/") },
      // inspect-components: exports whose target ≠ `src/<subpath>[/index]`
      // must be listed before the catch-all prefix rule.
      {
        find: /^@tsmono\/inspect-components\/chat\/tools$/,
        replacement: tsmono("inspect-components/src/chat/tools/tool.ts"),
      },
      {
        find: /^@tsmono\/inspect-components\/chat\/tools\/custom$/,
        replacement: tsmono("inspect-components/src/chat/tools/customToolRendering.tsx"),
      },
      {
        find: /^@tsmono\/inspect-components\/transcript\/timeline$/,
        replacement: tsmono("inspect-components/src/transcript/timeline/logic.ts"),
      },
      {
        find: /^@tsmono\/inspect-components\/transcript\/timeline\/swimlanes$/,
        replacement: tsmono("inspect-components/src/transcript/timeline/components/TimelineSwimLanes.tsx"),
      },
      { find: /^@tsmono\/inspect-components$/, replacement: tsmono("inspect-components/src/index.ts") },
      { find: /^@tsmono\/inspect-components\//, replacement: tsmono("inspect-components/src/") },
    ],
  },
  server: {
    // Proxy /ws/* + /sessions to the backend so only the Vite port needs
    // forwarding. ``VITE_BACKEND`` lets ``_screenshot_m1``/smoke fixtures
    // run the backend on a free port without 500ing the sidebar fetch.
    proxy: {
      "/ws": {
        target: process.env.VITE_BACKEND?.replace(/^http/, "ws") ?? "ws://127.0.0.1:8765",
        ws: true,
      },
      "/sessions": process.env.VITE_BACKEND ?? "http://127.0.0.1:8765",
    },
  },
  test: {
    globals: true,
    environment: "node",
  },
});
