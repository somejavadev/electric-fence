import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

type IconSet = {
  getIcon: (name: string) => Promise<unknown>;
};
type IconsWindow = Window & { customIcons?: Record<string, IconSet> };

// Stand-in for the frontend's `ha-icon`: the fields the refresh relies on.
class FakeHaIcon extends HTMLElement {
  icon = "";
  _legacy = true;
  requestUpdate = vi.fn();
}

beforeAll(() => {
  customElements.define("ha-icon", FakeHaIcon);
});

afterEach(() => {
  document.body.replaceChildren();
  delete (window as IconsWindow).customIcons;
  vi.resetModules();
});

async function load() {
  return import("../src/electric-fence-icons.ts");
}

function icon(name: string): FakeHaIcon {
  const el = document.createElement("ha-icon") as FakeHaIcon;
  el.icon = name;
  return el;
}

// Two shadow roots deep, as the sidebar sits inside the app's shadow DOM.
function nested(child: Element): void {
  const outer = document.createElement("div");
  const inner = document.createElement("div");
  outer.attachShadow({ mode: "open" }).append(inner);
  inner.attachShadow({ mode: "open" }).append(child);
  document.body.append(outer);
}

describe("electric-fence icon set", () => {
  it("registers the icon set on import", async () => {
    await load();
    const sets = (window as IconsWindow).customIcons;
    expect(typeof sets?.["electric-fence"]?.getIcon).toBe("function");
  });

  it("returns the glyph for 'logo' with a 24 viewBox", async () => {
    const { GLYPH_PATH, getIcon } = await load();
    expect(await getIcon("logo")).toEqual({
      path: GLYPH_PATH,
      viewBox: "0 0 24 24",
    });
  });

  it.each(["", "fence", "__proto__", "constructor", "LOGO"])(
    "returns undefined for %j",
    async (name) => {
      const { getIcon } = await load();
      expect(await getIcon(name)).toBeUndefined();
    },
  );

  it("uses the path from brand-src/sidebar-glyph.svg", async () => {
    const { GLYPH_PATH } = await load();
    const svg = readFileSync(
      // Vitest runs from frontend/.
      resolve(process.cwd(), "../brand-src/sidebar-glyph.svg"),
      "utf8",
    );
    expect(svg).toContain(`d="${GLYPH_PATH}"`);
  });

  it("keeps icon sets registered by others", async () => {
    const other: IconSet = { getIcon: async () => undefined };
    (window as IconsWindow).customIcons = { other };
    await load();
    expect((window as IconsWindow).customIcons?.["other"]).toBe(other);
  });

  it("refreshes an ha-icon that rendered before the set existed", async () => {
    const el = icon("electric-fence:logo");
    nested(el);
    await load();
    expect(el._legacy).toBe(false);
    expect(el.requestUpdate).toHaveBeenCalledWith("icon", undefined);
  });

  it("counts the icons it refreshed", async () => {
    const { refreshRenderedIcons } = await load();
    nested(icon("electric-fence:logo"));
    document.body.append(icon("electric-fence:logo"));
    expect(refreshRenderedIcons()).toBe(2);
  });

  it("leaves other icons alone", async () => {
    const other = icon("mdi:fence");
    nested(other);
    await load();
    expect(other._legacy).toBe(true);
    expect(other.requestUpdate).not.toHaveBeenCalled();
  });

  it("does not add a _legacy field the element does not have", async () => {
    const el = icon("electric-fence:logo");
    delete (el as Partial<FakeHaIcon>)._legacy;
    nested(el);
    await load();
    expect("_legacy" in el).toBe(false);
    expect(el.requestUpdate).toHaveBeenCalledWith("icon", undefined);
  });
});
