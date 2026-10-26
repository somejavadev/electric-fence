import type { CSSResultGroup } from "lit";
import { afterEach, describe, expect, it } from "vitest";
import { EfAnswerCard } from "../src/components/ef-answer-card.ts";
import { EfFilterBar } from "../src/components/ef-filter-bar.ts";
import { EfFilterPane } from "../src/components/ef-filter-pane.ts";
import { EfLoginRow } from "../src/components/ef-login-row.ts";
import { EfPostureList } from "../src/components/ef-posture-list.ts";
import { EfOverview } from "../src/views/ef-overview.ts";
import { panelStyles, rowStyles, viewStyles } from "../src/styles.ts";
import type { PostureItem } from "../src/types.ts";

// Home Assistant's styling: its design tokens for type, and its look for
// cards, labels and icons, so the panel follows the user's theme and text size.

function cssText(styles: CSSResultGroup | undefined): string {
  return ([styles] as unknown[])
    .flat(Infinity)
    .map((s) =>
      s && typeof s === "object" && "cssText" in s ? String(s.cssText) : "",
    )
    .join("\n");
}

const SHEETS: Record<string, string> = {
  panelStyles: cssText(panelStyles),
  rowStyles: cssText(rowStyles),
  viewStyles: cssText(viewStyles),
  "ef-answer-card": cssText(EfAnswerCard.styles),
  "ef-filter-bar": cssText(EfFilterBar.styles),
  "ef-filter-pane": cssText(EfFilterPane.styles),
  "ef-login-row": cssText(EfLoginRow.styles),
  "ef-posture-list": cssText(EfPostureList.styles),
  "ef-overview": cssText(EfOverview.styles),
};

function declarations(css: string, property: string): string[] {
  const re = new RegExp(`(?:^|[;{\\s])${property}\\s*:\\s*([^;}]+)`, "g");
  return [...css.matchAll(re)].map((m) => (m[1] ?? "").trim());
}

function rules(css: string, selector: RegExp): string[] {
  return [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)]
    .filter((m) => selector.test(m[1] ?? ""))
    .map((m) => m[2] ?? "");
}

describe("Home Assistant styling", () => {
  it.each(Object.entries(SHEETS))(
    "%s sets font sizes with Home Assistant tokens",
    (_name, css) => {
      for (const value of declarations(css, "font-size")) {
        expect(value).toMatch(
          /^var\(--ha-(font-size-[a-z0-9]+|card-header-font-size)\b/,
        );
      }
    },
  );

  it.each(Object.entries(SHEETS))(
    "%s sets line heights with Home Assistant tokens",
    (_name, css) => {
      for (const value of declarations(css, "line-height")) {
        expect(value).toMatch(/^var\(--ha-line-height-[a-z]+\b/);
      }
    },
  );

  it.each(["rowStyles", "viewStyles"])(
    "%s draws tags as tinted labels with readable text",
    (name) => {
      const css = SHEETS[name] ?? "";
      const tagRules = rules(css, /\.tag\b/);
      expect(tagRules.length).toBeGreaterThan(0);
      for (const body of tagRules) {
        // Status colours are too light for small text on a white card.
        expect(body).not.toMatch(
          /(^|[;\s])color\s*:\s*var\(--(error|warning|success)-color/,
        );
        expect(body).not.toMatch(/border-color\s*:/);
      }
      for (const status of ["error", "warning", "success"]) {
        expect(css).toMatch(
          new RegExp(`background[^;]*color-mix\\([^;]*--${status}-color`),
        );
      }
    },
  );

  it("gives answer cards a Home Assistant card header", () => {
    const button = rules(SHEETS["ef-answer-card"] ?? "", /^\s*button\s*$/);
    expect(button.join(";")).toMatch(
      /font-size\s*:\s*var\(--ha-card-header-font-size,\s*var\(--ha-font-size-2xl/,
    );
  });
});

describe("ef-posture-list icons", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  it("uses Material Design icons, not text characters", async () => {
    const el = document.createElement("ef-posture-list");
    const items: PostureItem[] = [
      { id: "admins_without_mfa", level: "ok", params: { users: [] } },
      {
        id: "auto_ban_off",
        level: "warning",
        params: { threshold: null, electric_fence: false },
      },
      {
        id: "auto_ban_off",
        level: "error",
        params: { threshold: null, electric_fence: false },
      },
    ];
    el.items = items;
    document.body.append(el);
    await el.updateComplete;
    const icons = [...(el.shadowRoot?.querySelectorAll("ha-icon") ?? [])];
    expect(icons.map((i) => i.getAttribute("icon"))).toEqual([
      "mdi:check-circle-outline",
      "mdi:alert-circle-outline",
      "mdi:close-circle-outline",
    ]);
    expect(el.shadowRoot?.textContent).not.toMatch(/[✓!✕]/);
  });
});
