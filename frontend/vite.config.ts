import { cpSync, copyFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

const outDir = "../custom_components/electric_fence/frontend_dist";

export default defineConfig({
  plugins: [
    {
      name: "copy-flags",
      writeBundle() {
        const flags = resolve(outDir, "flags");
        cpSync(resolve("node_modules/flag-icons/flags/4x3"), flags, {
          recursive: true,
          filter: (src) => src.endsWith("4x3") || /\/[a-z]{2}\.svg$/.test(src),
        });
        // MIT: the permission notice travels with the copies.
        copyFileSync(
          resolve("node_modules/flag-icons/LICENSE"),
          resolve(flags, "LICENSE"),
        );
      },
    },
  ],
  build: {
    target: "es2022",
    outDir,
    emptyOutDir: true,
    lib: {
      entry: {
        "electric-fence-panel": "src/electric-fence-panel.ts",
        // Loaded on every page for the sidebar icon, so it stays separate.
        "electric-fence-icons": "src/electric-fence-icons.ts",
      },
      formats: ["es"],
      fileName: (_format, name) => `${name}.js`,
    },
  },
  test: {
    environment: "happy-dom",
    include: ["test/**/*.test.ts"],
    // lcov for SonarCloud (frontend/coverage/lcov.info); see sonar-project.properties.
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      // Paths relative to the repository root, as SonarCloud resolves them.
      reporter: ["text-summary", ["lcov", { projectRoot: ".." }]],
    },
  },
});
