import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/ws": {
        target: "http://127.0.0.1:8000",
        ws: true,
      },
      "/api": {
        target: "http://127.0.0.1:8000",
      },
    },
  },
});
