import js from "@eslint/js";
import globals from "globals";
import lit from "eslint-plugin-lit";
import wc from "eslint-plugin-wc";
import tseslint from "typescript-eslint";

const htmlSinks = ["innerHTML", "outerHTML", "insertAdjacentHTML"];

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
          paths: [
            "lit/directives/unsafe-html.js",
            "lit/directives/unsafe-svg.js",
          ].map((name) => ({
            name,
            message: "Never render untrusted strings as markup.",
          })),
        },
      ],
      "no-restricted-properties": [
        "error",
        ...htmlSinks.map((property) => ({
          property,
          message: "Use text content or a Lit template instead.",
        })),
        {
          object: "document",
          property: "write",
          message: "Never write markup into the document.",
        },
        {
          object: "document",
          property: "writeln",
          message: "Never write markup into the document.",
        },
      ],
      "no-restricted-syntax": [
        "error",
        ...htmlSinks.map((name) => ({
          selector: `Literal[value='${name}']`,
          message: `Do not reach for ${name} by name.`,
        })),
      ],
    },
  },
  {
    files: ["*.js", "*.ts"],
    languageOptions: { globals: globals.node },
  },
);
