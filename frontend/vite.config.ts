import { defineConfig } from "vitest/config";

export default defineConfig({
  build: {
    target: "es2022",
    outDir: "../custom_components/electric_fence/frontend_dist",
    emptyOutDir: true,
    lib: {
      entry: "src/electric-fence-panel.ts",
      formats: ["es"],
      fileName: () => "electric-fence-panel.js",
    },
  },
  test: {
    environment: "happy-dom",
    include: ["test/**/*.test.ts"],
  },
});
