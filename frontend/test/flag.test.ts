import { afterEach, describe, expect, it } from "vitest";
import "../src/components/ef-flag.ts";
import type { EfFlag } from "../src/components/ef-flag.ts";

async function flag(code: string | null): Promise<EfFlag> {
  const el = document.createElement("ef-flag");
  el.code = code;
  document.body.append(el);
  await el.updateComplete;
  return el;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("ef-flag", () => {
  it("renders a decorative local image for NL", async () => {
    const img = (await flag("NL")).shadowRoot?.querySelector("img");
    expect(img?.getAttribute("src")).toMatch(/\/flags\/nl\.svg$/);
    expect(img?.getAttribute("alt")).toBe("");
    expect(img?.getAttribute("width")).toBe("20");
    expect(img?.getAttribute("height")).toBe("15");
  });

  it.each([null, "", "NLD", "1!", "../x"])(
    "renders nothing for %j",
    async (code) => {
      const el = await flag(code);
      expect(el.shadowRoot?.querySelector("img")).toBeNull();
    },
  );

  it("hides itself when the image fails to load", async () => {
    const el = await flag("ZZ");
    el.shadowRoot?.querySelector("img")?.dispatchEvent(new Event("error"));
    expect(el.hidden).toBe(true);
  });

  it("shows again after the code changes", async () => {
    const el = await flag("ZZ");
    el.shadowRoot?.querySelector("img")?.dispatchEvent(new Event("error"));
    el.code = "NL";
    await el.updateComplete;
    expect(el.hidden).toBe(false);
  });
});
