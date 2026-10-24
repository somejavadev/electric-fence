// @vitest-environment node
import { ESLint } from "eslint";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Static bans only: a runtime-computed key (`el[name] = html`) cannot be
// found by a linter. Untrusted strings must reach the DOM through Lit
// templates, which escape them.
const root = fileURLToPath(new URL("..", import.meta.url));
const eslint = new ESLint({
  cwd: root,
  overrideConfigFile: join(root, "eslint.config.js"),
});

const PROBES: Record<string, string> = {
  "lit unsafe-html": `import { unsafeHTML } from "lit/directives/unsafe-html.js"; unsafeHTML("");`,
  "lit unsafe-svg": `import { unsafeSVG } from "lit/directives/unsafe-svg.js"; unsafeSVG("");`,
  "lit-html unsafe-html": `import { unsafeHTML } from "lit-html/directives/unsafe-html.js"; unsafeHTML("");`,
  "lit-html root": `import { html } from "lit-html"; html\`\`;`,
  "lit static-html": `import { unsafeStatic } from "lit/static-html.js"; unsafeStatic("");`,
  "lit-html static": `import { literal } from "lit-html/static.js"; literal\`\`;`,
  innerHTML: `export const f = (e: HTMLElement) => { e.innerHTML = "x"; };`,
  outerHTML: `export const f = (e: HTMLElement) => { e.outerHTML = "x"; };`,
  insertAdjacentHTML: `export const f = (e: HTMLElement) => { e.insertAdjacentHTML("beforeend", "x"); };`,
  "computed innerHTML": `export const f = (e: HTMLElement) => { e["innerHTML"] = "x"; };`,
  "computed template key": `export const f = (e: HTMLElement) => { e[\`outerHTML\`] = "x"; };`,
  "document.write": `document.write("x");`,
  "window.document.write": `window.document.write("x");`,
  "document.writeln": `document.writeln("x");`,
  "aliased document.write": `const d = document; d.write("x");`,
  createContextualFragment: `export const f = (r: Range) => r.createContextualFragment("x");`,
  parseFromString: `new DOMParser().parseFromString("x", "text/html");`,
  eval: `eval("1");`,
  "implied eval": `setTimeout("1", 0);`,
  "new Function": `new Function("1");`,
};

describe("lint bans", () => {
  it.each(Object.entries(PROBES))("rejects %s", async (_name, code) => {
    const [result] = await eslint.lintText(code, {
      filePath: "src/probe.ts",
    });
    expect(result?.messages.some((m) => m.severity === 2)).toBe(true);
  });

  it("accepts a plain Lit import", async () => {
    const [result] = await eslint.lintText(
      `import { html } from "lit"; export const t = html\`<p>\${"x"}</p>\`;`,
      { filePath: "src/probe.ts" },
    );
    expect(result?.messages).toEqual([]);
  });
});
