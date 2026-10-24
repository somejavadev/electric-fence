import { afterEach, describe, expect, it } from "vitest";
import "../src/components/ef-login-row.ts";
import { EfLoginRow } from "../src/components/ef-login-row.ts";
import type { HomeAssistant } from "../src/types.ts";
import { login } from "./helpers.ts";

const hass = {
  locale: { language: "en" },
  config: { time_zone: "UTC" },
  language: "en",
  connection: {},
} as unknown as HomeAssistant;

const HOSTILE = [
  "<img src=x onerror=alert(1)>",
  "javascript:alert(1)",
  "‮evil",
  "a".repeat(10_000),
];

async function row(
  typed: string,
  variant: "failed" | "success" = "failed",
): Promise<EfLoginRow> {
  const el = document.createElement("ef-login-row");
  el.hass = hass;
  el.variant = variant;
  el.item = login({ username_typed: typed, client_name: typed });
  document.body.append(el);
  await el.updateComplete;
  return el;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("hostile typed usernames", () => {
  it.each(HOSTILE)("%#: stays text and is truncated", async (typed) => {
    for (const variant of ["failed", "success"] as const) {
      const el = await row(typed, variant);
      const root = el.shadowRoot as ShadowRoot;
      expect(root.querySelector("img")).toBeNull();
      expect(root.querySelector("[onerror]")).toBeNull();
      expect(root.querySelector("a[href]")).toBeNull();
      expect(root.querySelector('[href^="javascript:" i]')).toBeNull();
      const name = root.querySelector<HTMLElement>(".username");
      const visible = name?.textContent ?? "";
      expect(visible.length).toBeLessThanOrEqual(65);
      expect(name?.getAttribute("title")).toBe(typed);
      if (typed.length <= 64) {
        expect(root.textContent).toContain(typed);
      } else {
        expect(visible.startsWith("a".repeat(64))).toBe(true);
      }
      document.body.replaceChildren();
    }
  });
});
