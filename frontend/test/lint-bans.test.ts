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
  "innerHTML call or assignment": `export const f = (e: HTMLElement) => { e.innerHTML = "x"; };`,
  "outerHTML call or assignment": `export const f = (e: HTMLElement) => { e.outerHTML = "x"; };`,
  "insertAdjacentHTML call or assignment": `export const f = (e: HTMLElement) => { e.insertAdjacentHTML("beforeend", "x"); };`,
  "computed innerHTML": `export const f = (e: HTMLElement) => { e["innerHTML"] = "x"; };`,
  "computed template key": `export const f = (e: HTMLElement) => { e[\`outerHTML\`] = "x"; };`,
  "document.write": `document.write("x");`,
  "window.document.write": `window.document.write("x");`,
  "document.writeln": `document.writeln("x");`,
  "aliased document.write": `const d = document; d.write("x");`,
  "createContextualFragment call or assignment": `export const f = (r: Range) => r.createContextualFragment("x");`,
  "parseFromString call or assignment": `new DOMParser().parseFromString("x", "text/html");`,
  "dynamic import unsafe-html": `export const f = () => import("lit/directives/unsafe-html.js");`,
  "dynamic import lit-html": `export const f = () => import("lit-html");`,
  "dynamic import static-html": `export const f = () => import("lit/static-html.js");`,
  "dynamic import template": `export const f = () => import(\`lit/directives/unsafe-svg.js\`);`,
  "setHTMLUnsafe call": `export const f = (e: Element) => { e.setHTMLUnsafe("x"); };`,
  "computed setHTMLUnsafe": `export const f = (e: Element) => { e["setHTMLUnsafe"]("x"); };`,
  "srcdoc assignment": `export const f = (e: HTMLIFrameElement) => { e.srcdoc = "x"; };`,
  "computed srcdoc assignment": `export const f = (e: HTMLIFrameElement) => { e["srcdoc"] = "x"; };`,
  "destructured innerHTML": `export const f = (e: HTMLElement) => { const { innerHTML } = e; return innerHTML; };`,
  "destructured renamed sink": `export const f = (e: HTMLElement) => { const { insertAdjacentHTML: put } = e; put("beforeend", "x"); };`,
  "destructured srcdoc": `export const f = (e: HTMLIFrameElement) => { const { srcdoc } = e; return srcdoc; };`,
  "destructured parameter": `export const f = ({ outerHTML }: HTMLElement) => outerHTML;`,
  "destructured computed template": `export const f = (e: HTMLElement) => { const { [\`innerHTML\`]: x } = e; return x; };`,
  "setAttribute srcdoc": `export const f = (e: HTMLElement) => { e.setAttribute("srcdoc", "x"); };`,
  "Reflect.set srcdoc": `export const f = (e: HTMLElement) => { Reflect.set(e, "srcdoc", "x"); };`,
  "string-key destructuring": `export const f = (e: HTMLElement) => { const { "innerHTML": x } = e; return x; };`,
  "setHTMLUnsafe string": `export const f = (e: HTMLElement) => Reflect.get(e, "setHTMLUnsafe");`,
  "Object.assign srcdoc": `export const f = (e: HTMLElement) => Object.assign(e, { srcdoc: "x" });`,
  "object literal innerHTML key": `export const f = (e: HTMLElement) => Object.assign(e, { innerHTML: "x" });`,
  "object literal string key": `export const f = (e: HTMLElement) => Object.assign(e, { "outerHTML": "x" });`,
  "object literal template key": `export const f = (e: HTMLElement) => Object.assign(e, { [\`srcdoc\`]: "x" });`,
  "dynamic import of a variable": `export const f = (m: string) => import(m);`,
  "dynamic import of a concatenation": `export const f = () => import("lit/directives/unsafe-" + "html.js");`,
  "lit property binding srcdoc": `import { html } from "lit"; export const t = (x: string) => html\`<iframe .srcdoc=\${x}></iframe>\`;`,
  "lit property binding innerHTML": `import { html } from "lit"; export const t = (x: string) => html\`<div .innerHTML=\${x}></div>\`;`,
  "lit property binding outerHTML spaced": `import { html } from "lit"; export const t = (x: string) => html\`<div .outerHTML = \${x}></div>\`;`,
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

  it("accepts a static dynamic import of an allowed module", async () => {
    const [result] = await eslint.lintText(
      `export const f = () => import("./other.ts");`,
      { filePath: "src/probe.ts" },
    );
    expect(result?.messages).toEqual([]);
  });

  it("accepts a plain Lit import", async () => {
    const [result] = await eslint.lintText(
      `import { html } from "lit"; export const t = html\`<p>\${"x"}</p>\`;`,
      { filePath: "src/probe.ts" },
    );
    expect(result?.messages).toEqual([]);
  });
});
