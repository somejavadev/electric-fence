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
];
const sinkPattern = `/^(${markupSinks.join("|")})$/`;

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
          selector: `Literal[value=${sinkPattern}]`,
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
