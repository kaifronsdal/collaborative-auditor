/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // `@tsmono/util`'s barrel re-exports `arrow.ts` which pulls heavy deps
    // (flechette/arquero/lz4js) that inspect-view has but we don't. We only
    // reach the barrel transitively via `@tsmono/inspect-components/chat`
    // for `isJson`/`decodeHtmlEntities`; stub the unused optional deps so
    // the dev server doesn't 500 on the barrel import.
    alias: {
      "@uwdata/flechette": new URL("./src/lib/empty-stub.ts", import.meta.url).pathname,
      arquero: new URL("./src/lib/empty-stub.ts", import.meta.url).pathname,
      lz4js: new URL("./src/lib/empty-stub.ts", import.meta.url).pathname,
      json5: new URL("./src/lib/empty-stub.ts", import.meta.url).pathname,
    },
  },
  server: {
    // Proxy /ws/* to the backend so only the Vite port needs forwarding.
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
      "/sessions": "http://127.0.0.1:8765",
    },
  },
  test: {
    globals: true,
    environment: "node",
  },
});
