import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EfFilterPane } from "../src/components/ef-filter-pane.ts";
import type {
  FilterChange,
  FilterField,
} from "../src/components/ef-filter-pane.ts";

const FIELDS: FilterField[] = [
  {
    name: "kind",
    label: "Kind",
    type: "select",
    options: [
      { value: "", label: "Any kind" },
      { value: "user", label: "Users" },
    ],
  },
  {
    name: "range",
    label: "Time range",
    type: "select",
    options: [
      { value: "7d", label: "Last 7 days" },
      { value: "custom", label: "Custom range" },
    ],
  },
  {
    name: "from",
    label: "From",
    type: "date",
    when: { field: "range", equals: "custom" },
  },
  {
    name: "to",
    label: "To",
    type: "date",
    when: { field: "range", equals: "custom" },
  },
  { name: "ip", label: "IP address", type: "text" },
  { name: "system", label: "Show system changes", type: "checkbox" },
];

const DEFAULTS = { range: "7d" };

async function mount(
  values: Record<string, string> = { range: "7d" },
  extra: Partial<EfFilterPane> = {},
): Promise<EfFilterPane> {
  const el = document.createElement("ef-filter-pane");
  el.fields = FIELDS;
  el.values = values;
  el.defaults = DEFAULTS;
  Object.assign(el, extra);
  document.body.append(el);
  await el.updateComplete;
  return el;
}

const root = (el: EfFilterPane): ShadowRoot => el.shadowRoot as ShadowRoot;

function badge(el: EfFilterPane): string | undefined {
  return root(el).querySelector(".badge")?.textContent.trim();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("ef-filter-pane", () => {
  it("counts only values that differ from the default", async () => {
    const el = await mount({ range: "7d" });
    expect(badge(el)).toBeUndefined();
    el.values = { range: "7d", kind: "user", ip: "1.2.3.4", system: "" };
    await el.updateComplete;
    expect(badge(el)).toBe("2");
    el.values = { range: "custom", kind: "user" };
    await el.updateComplete;
    expect(badge(el)).toBe("2");
  });

  it("does not count a default that is not an empty string", async () => {
    const el = await mount({});
    expect(badge(el)).toBeUndefined();
  });

  it("opens and closes from the Filters chip", async () => {
    const el = await mount();
    const chip = root(el).querySelector<HTMLButtonElement>("button.chip");
    expect(chip?.textContent).toContain("Filters");
    expect(root(el).querySelector(".pane")).toBeNull();
    chip?.click();
    await el.updateComplete;
    expect(el.open).toBe(true);
    expect(root(el).querySelector(".pane")).not.toBeNull();
    expect(chip?.getAttribute("aria-expanded")).toBe("true");
    chip?.click();
    await el.updateComplete;
    expect(el.open).toBe(false);
  });

  it("Clear emits filters-cleared and drops a pending search", async () => {
    const el = await mount({ range: "custom", kind: "user" }, { open: true });
    const cleared = vi.fn();
    const changed = vi.fn();
    el.addEventListener("filters-cleared", cleared);
    el.addEventListener("filter-changed", changed);
    const search = root(el).querySelector<HTMLInputElement>("[name=search]");
    (search as HTMLInputElement).value = "abc";
    search?.dispatchEvent(new Event("input", { bubbles: true }));
    root(el).querySelector<HTMLButtonElement>("button.clear")?.click();
    vi.advanceTimersByTime(1000);
    expect(cleared).toHaveBeenCalledTimes(1);
    expect(changed).not.toHaveBeenCalled();
    expect(search?.value).toBe("");
  });

  it("emits one search after 300 ms for three quick keystrokes", async () => {
    const el = await mount();
    const seen: FilterChange[] = [];
    el.addEventListener("filter-changed", (e) =>
      seen.push((e as CustomEvent<FilterChange>).detail),
    );
    const search = root(el).querySelector<HTMLInputElement>(
      "input[name=search]",
    ) as HTMLInputElement;
    for (const text of ["a", "ab", " abc "]) {
      search.value = text;
      search.dispatchEvent(new Event("input", { bubbles: true }));
      vi.advanceTimersByTime(100);
    }
    expect(seen).toEqual([]);
    vi.advanceTimersByTime(150);
    expect(seen).toEqual([]);
    vi.advanceTimersByTime(60);
    expect(seen).toEqual([{ name: "search", value: "abc" }]);
  });

  it("shows the search value from values", async () => {
    const el = await mount({ range: "7d", search: "needle" });
    const search = root(el).querySelector<HTMLInputElement>("[name=search]");
    expect(search?.value).toBe("needle");
  });

  it("renders a bottom sheet when narrow", async () => {
    const wide = await mount(undefined, { open: true });
    expect(root(wide).querySelector(".sheet")).toBeNull();
    expect(root(wide).querySelector(".pane")).not.toBeNull();
    const narrow = await mount(undefined, { open: true, narrow: true });
    expect(root(narrow).querySelector(".sheet")).not.toBeNull();
    expect(root(narrow).querySelector(".pane")).toBeNull();
  });

  it("styles the pane at 280 px and the sheet fixed at 70vh", () => {
    const css = String(
      (EfFilterPane.styles as unknown as { cssText: string }).cssText,
    );
    expect(css).toMatch(/width\s*:\s*280px/);
    expect(css).toMatch(/position\s*:\s*fixed/);
    expect(css).toMatch(/max-height\s*:\s*70vh/);
    expect(css).toMatch(/border-radius\s*:\s*10px/);
  });

  it("shows from and to only with the custom range", async () => {
    const names = (el: EfFilterPane) =>
      [...root(el).querySelectorAll("details")].map((d) =>
        d.querySelector("summary .label")?.textContent.trim(),
      );
    const el = await mount({ range: "7d" }, { open: true });
    expect(names(el)).toEqual([
      "Kind",
      "Time range",
      "IP address",
      "Show system changes",
    ]);
    el.values = { range: "custom" };
    await el.updateComplete;
    expect(names(el)).toEqual([
      "Kind",
      "Time range",
      "From",
      "To",
      "IP address",
      "Show system changes",
    ]);
    expect(
      root(el).querySelector("input[type=date][name=from]"),
    ).not.toBeNull();
  });

  it("emits filter-changed for selects, dates, text and checkboxes", async () => {
    const el = await mount({ range: "custom" }, { open: true });
    const seen: FilterChange[] = [];
    el.addEventListener("filter-changed", (e) =>
      seen.push((e as CustomEvent<FilterChange>).detail),
    );
    const kind = root(el).querySelector<HTMLSelectElement>("select[name=kind]");
    (kind as HTMLSelectElement).value = "user";
    kind?.dispatchEvent(new Event("change", { bubbles: true }));
    const from = root(el).querySelector<HTMLInputElement>("[name=from]");
    (from as HTMLInputElement).value = "2026-10-01";
    from?.dispatchEvent(new Event("change", { bubbles: true }));
    const ip = root(el).querySelector<HTMLInputElement>("[name=ip]");
    (ip as HTMLInputElement).value = " 1.2.3.4 ";
    ip?.dispatchEvent(new Event("change", { bubbles: true }));
    const box = root(el).querySelector<HTMLInputElement>("[name=system]");
    (box as HTMLInputElement).checked = true;
    box?.dispatchEvent(new Event("change", { bubbles: true }));
    expect(seen).toEqual([
      { name: "kind", value: "user" },
      { name: "from", value: "2026-10-01" },
      { name: "ip", value: "1.2.3.4" },
      { name: "system", value: "1" },
    ]);
  });

  it("shows the current value in each summary", async () => {
    const el = await mount(
      { range: "7d", kind: "user", ip: "9.9.9.9", system: "1" },
      { open: true },
    );
    const values = [...root(el).querySelectorAll("summary .value")].map((v) =>
      v.textContent.trim(),
    );
    expect(values).toEqual(["Users", "Last 7 days", "9.9.9.9", "On"]);
  });

  it("renders hostile labels and values as text", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const el = await mount(
      { range: "7d", kind: "bad", ip: hostile },
      { open: true },
    );
    el.fields = [
      {
        name: "kind",
        label: hostile,
        type: "select",
        options: [{ value: "bad", label: hostile }],
      },
      { name: "ip", label: hostile, type: "text" },
    ];
    await el.updateComplete;
    expect(root(el).querySelector("img")).toBeNull();
    expect(root(el).querySelector("[onerror]")).toBeNull();
    const labels = [...root(el).querySelectorAll("summary .label")];
    expect(labels.map((l) => l.textContent.trim())).toEqual([hostile, hostile]);
    const values = [...root(el).querySelectorAll("summary .value")];
    expect(values.map((v) => v.textContent.trim())).toEqual([hostile, hostile]);
  });

  it("offers an empty actions slot after the header controls", async () => {
    const el = await mount();
    const slot = root(el).querySelector<HTMLSlotElement>("slot[name=actions]");
    expect(slot).not.toBeNull();
    expect(slot?.assignedNodes()).toEqual([]);
    expect(slot?.closest(".header")).not.toBeNull();
    expect(root(el).querySelector("slot:not([name])")).not.toBeNull();
  });
});
