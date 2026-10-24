import { afterEach, beforeAll, describe, expect, it } from "vitest";
import {
  ElectricFencePanel,
  tabFromPath,
} from "../src/electric-fence-panel.ts";
import type { HomeAssistant } from "../src/types.ts";

// `ha-menu-button` exists only in the Home Assistant frontend.
beforeAll(() => {
  customElements.define("ha-menu-button", class extends HTMLElement {});
});

const hass = {
  locale: { language: "en" },
  config: { time_zone: "UTC" },
  language: "en",
  connection: {},
} as unknown as HomeAssistant;

async function mount(
  path: string,
  narrow = false,
): Promise<ElectricFencePanel> {
  const el = document.createElement("electric-fence-panel");
  el.hass = hass;
  el.narrow = narrow;
  el.route = { prefix: "/electric-fence", path };
  document.body.append(el);
  await el.updateComplete;
  return el;
}

afterEach(() => {
  document.body.replaceChildren();
});

const tabs = (el: ElectricFencePanel) => [
  ...(el.shadowRoot?.querySelectorAll<HTMLElement>("[role=tab]") ?? []),
];

describe("electric-fence-panel", () => {
  it("shows the title and five tabs, overview by default", async () => {
    const el = await mount("");
    expect(el.shadowRoot?.querySelector("h1")?.textContent).toBe(
      "Electric Fence",
    );
    expect(tabs(el).map((t) => t.textContent.trim())).toEqual([
      "Overview",
      "Activity",
      "Users",
      "Audit",
      "Settings",
    ]);
    const selected = tabs(el).filter(
      (t) => t.getAttribute("aria-selected") === "true",
    );
    expect(selected.map((t) => t.textContent.trim())).toEqual(["Overview"]);
  });

  it("selects the tab named by route.path", async () => {
    const el = await mount("/audit");
    const selected = tabs(el).filter(
      (t) => t.getAttribute("aria-selected") === "true",
    );
    expect(selected.map((t) => t.textContent.trim())).toEqual(["Audit"]);
  });

  it("follows route changes", async () => {
    const el = await mount("/overview");
    el.route = { prefix: "/electric-fence", path: "/users" };
    await el.updateComplete;
    expect(
      tabs(el)
        .find((t) => t.getAttribute("aria-selected") === "true")
        ?.textContent.trim(),
    ).toBe("Users");
  });

  it("always renders the menu button; Home Assistant hides it itself", async () => {
    for (const narrow of [false, true]) {
      document.body.replaceChildren();
      const el = await mount("", narrow);
      expect(el.shadowRoot?.querySelector("ha-menu-button")).not.toBeNull();
    }
  });

  it("falls back to overview for unknown paths", () => {
    expect(tabFromPath("/nonsense")).toBe("overview");
    expect(tabFromPath(undefined)).toBe("overview");
    expect(tabFromPath("/settings/x")).toBe("settings");
  });
});
