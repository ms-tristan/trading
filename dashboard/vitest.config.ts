import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Vitest configuration for the monitoring dashboard.
 *
 * This file is part of the shared test policy (docs/testing-policy.md): the
 * coverage thresholds below are the dashboard gate of `.github/workflows/ci.yml`
 * and of `npm run test:coverage`. Work packages must not edit it; a package that
 * needs a new source file simply puts it under `src/` and tests it there.
 *
 * - jsdom environment, because every component test renders DOM;
 * - the `@/` alias mirrors the `paths` entry of tsconfig.json, so tests import
 *   exactly what the application imports;
 * - the setup file registers the jest-dom matchers on Vitest's `expect`;
 * - thresholds: lines/statements/functions >= 70 %, branches >= 60 %.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Resolved from the dashboard directory, where `npm run test:coverage` runs.
      "@": path.resolve(process.cwd(), "src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    restoreMocks: true,
    clearMocks: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.{test,spec}.{ts,tsx}", "src/test/**", "src/**/*.d.ts"],
      thresholds: {
        lines: 70,
        statements: 70,
        functions: 70,
        branches: 60,
      },
    },
  },
});
