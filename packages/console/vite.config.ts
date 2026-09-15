import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../../src/benchpress/console_dist", emptyOutDir: true },
  server: { proxy: { "/v1": "http://127.0.0.1:8787", "/healthz": "http://127.0.0.1:8787" } },
  test: { environment: "jsdom", globals: true },
});
