import js from "@eslint/js";
import globals from "globals";
import lit from "eslint-plugin-lit";
import wc from "eslint-plugin-wc";
import tseslint from "typescript-eslint";

// Property names that turn a string into markup or script. Static bans only:
// a runtime-computed key cannot be linted, so untrusted strings must reach the
// DOM through Lit templates, which escape them.
const markupSinks = [
  "innerHTML",
  "outerHTML",
  "insertAdjacentHTML",
  "write",
  "writeln",
  "createContextualFragment",
  "parseFromString",
  "setHTMLUnsafe",
  "parseHTMLUnsafe",
];
const sinkPattern = `/^(${markupSinks.join("|")})$/`;
// Names banned when destructured or assigned, beyond the member bans above.
const assignedSinkPattern = `/^(${[...markupSinks, "srcdoc"].join("|")})$/`;
// A Lit property binding such as `.innerHTML=${x}` inside an html template.
const litSinkBinding = String.raw`/\.(innerHTML|outerHTML|srcdoc|setHTMLUnsafe)\s*=/`;
// Modules that must not even be loaded lazily.
const bannedModulePattern = "/unsafe-html|unsafe-svg|lit-html|static-html/";

export default tseslint.config(
  { ignores: ["node_modules/**"] },
  js.configs.recommended,
  tseslint.configs.strict,
  lit.configs["flat/recommended"],
  wc.configs["flat/recommended"],
  {
    files: ["**/*.ts"],
    languageOptions: { globals: globals.browser },
    rules: {
      // Untrusted strings (usernames, user agents) must only ever be text.
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["**/unsafe-html*", "**/unsafe-svg*"],
              message: "Never render untrusted strings as markup.",
            },
            {
              group: ["lit/static-html*", "lit-html/static*"],
              message: "Static HTML templates take raw strings.",
            },
            {
              group: ["lit-html", "lit-html/**"],
              message: "Import from lit only.",
            },
          ],
        },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: `MemberExpression[property.name=${sinkPattern}]`,
          message: "Use a Lit template instead of writing markup.",
        },
        {
          selector: `MemberExpression[computed=true][property.value=${sinkPattern}]`,
          message: "Use a Lit template instead of writing markup.",
        },
        {
          selector: `MemberExpression[computed=true] > TemplateLiteral.property > TemplateElement[value.raw=${sinkPattern}]`,
          message: "Use a Lit template instead of writing markup.",
        },
        {
          selector: `ImportExpression[source.value=${bannedModulePattern}]`,
          message: "Never import markup-writing modules, not even lazily.",
        },
        {
          selector: `ImportExpression[source.type=TemplateLiteral] > TemplateLiteral.source > TemplateElement[value.raw=${bannedModulePattern}]`,
          message: "Never import markup-writing modules, not even lazily.",
        },
        {
          selector: `AssignmentExpression > MemberExpression.left[property.name="srcdoc"]`,
          message: "Do not assign srcdoc: it is markup.",
        },
        {
          selector: `AssignmentExpression > MemberExpression.left[computed=true][property.value="srcdoc"]`,
          message: "Do not assign srcdoc: it is markup.",
        },
        {
          selector: `ObjectPattern > Property[key.name=${assignedSinkPattern}]`,
          message: "Do not destructure markup sinks.",
        },
        {
          selector: `ObjectPattern > Property[computed=true] > TemplateLiteral.key > TemplateElement[value.raw=${assignedSinkPattern}]`,
          message: "Do not destructure markup sinks.",
        },
        {
          selector: "ImportExpression:not([source.type='Literal'])",
          message: "Dynamic imports must name a plain string module.",
        },
        {
          selector: `ObjectExpression > Property[key.name=${assignedSinkPattern}]`,
          message: "Do not write markup sinks through object keys.",
        },
        {
          selector: `ObjectExpression > Property[computed=true] > TemplateLiteral.key > TemplateElement[value.raw=${assignedSinkPattern}]`,
          message: "Do not write markup sinks through object keys.",
        },
        {
          selector: `TaggedTemplateExpression[tag.name=/^(html|svg)$/] > TemplateLiteral > TemplateElement[value.raw=${litSinkBinding}]`,
          message: "Do not bind markup sinks as Lit properties.",
        },
        {
          selector: `Literal[value=${assignedSinkPattern}]`,
          message: "Do not reach for markup sinks by name.",
        },
      ],
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
    },
  },
  {
    files: ["*.js", "vite.config.ts"],
    languageOptions: { globals: globals.node },
  },
);
