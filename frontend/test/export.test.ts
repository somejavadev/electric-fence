import { afterEach, describe, expect, it, vi } from "vitest";
import { collectAll, csvSafe, toCsv } from "../src/export.ts";
import { setMaskIps } from "../src/mask.ts";
import { EfActivity } from "../src/views/ef-activity.ts";
import { EfAudit } from "../src/views/ef-audit.ts";
import { change, fakeApi, login } from "./helpers.ts";
import type { FakeApi } from "./helpers.ts";

const BOM = String.fromCharCode(0xfeff);

const saved = vi.hoisted(() => ({
  calls: [] as { name: string; mime: string; body: string }[],
}));

vi.mock("../src/export.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/export.ts")>();
  return {
    ...actual,
    downloadFile: (name: string, mime: string, body: string) => {
      saved.calls.push({ name, mime, body });
    },
  };
});

afterEach(() => {
  setMaskIps(false);
  document.body.replaceChildren();
  saved.calls.length = 0;
});

describe("csvSafe", () => {
  it.each<[unknown, string]>([
    ['=HYPERLINK("x")', `"'=HYPERLINK(""x"")"`],
    ["+cmd", "'+cmd"],
    ["@SUM", "'@SUM"],
    ["-1", "'-1"],
    ["\tx", "'\tx"],
    ["\rx", '"\'\rx"'],
    ["a,b", '"a,b"'],
    ["line1\nline2", '"line1\nline2"'],
    ['say "hi"', '"say ""hi"""'],
    [" lead", '" lead"'],
    ["trail ", '"trail "'],
    ["ユーザー", "ユーザー"],
    [null, ""],
    [undefined, ""],
    [{ a: 1 }, '"{""a"":1}"'],
    [5, "5"],
    [true, "true"],
  ])("%j -> %s", (value, expected) => {
    expect(csvSafe(value)).toBe(expected);
  });
});

describe("toCsv", () => {
  it("starts with a BOM and uses CRLF", () => {
    const out = toCsv(
      ["a", "b"],
      [
        ["1", "x,y"],
        [null, 2],
      ],
    );
    expect(out).toBe(BOM + 'a,b\r\n1,"x,y"\r\n,2\r\n');
  });
});

describe("collectAll", () => {
  const pager = (pages: number[][]) => {
    const asked: (number | null)[] = [];
    const fetchPage = (before: number | null) => {
      asked.push(before);
      const i = asked.length - 1;
      return Promise.resolve({
        items: pages[i] ?? [],
        next_before_id:
          i < pages.length - 1 ? (pages[i]?.at(-1) ?? null) : null,
      });
    };
    return { asked, fetchPage };
  };

  it("follows next_before_id until null", async () => {
    const { asked, fetchPage } = pager([[5, 4], [3, 2], [1]]);
    const progress: number[] = [];
    const out = await collectAll(fetchPage, 100, (n) => progress.push(n));
    expect(out).toEqual({ items: [5, 4, 3, 2, 1], truncated: false });
    expect(asked).toEqual([null, 4, 2]);
    expect(progress).toEqual([2, 4, 5]);
  });

  it("stops at the cap and says so", async () => {
    const { fetchPage } = pager([[5, 4], [3, 2], [1]]);
    const out = await collectAll(fetchPage, 3);
    expect(out).toEqual({ items: [5, 4, 3], truncated: true });
  });

  it("is not truncated when the last page ends exactly at the cap", async () => {
    const { fetchPage } = pager([[2, 1]]);
    const out = await collectAll(fetchPage, 2);
    expect(out.truncated).toBe(false);
  });

  it("stops if the cursor does not advance", async () => {
    let calls = 0;
    const out = await collectAll(() => {
      calls += 1;
      return Promise.resolve({ items: [1], next_before_id: 7 });
    }, 100);
    expect(calls).toBeLessThan(5);
    expect(out.truncated).toBe(false);
  });
});

async function settle(el?: { updateComplete: Promise<unknown> }) {
  for (let i = 0; i < 12; i += 1) {
    await Promise.resolve();
  }
  await el?.updateComplete;
  for (let i = 0; i < 6; i += 1) {
    await Promise.resolve();
  }
  await el?.updateComplete;
}

async function mount<
  T extends HTMLElement & { updateComplete: Promise<unknown> },
>(tag: string, api: FakeApi): Promise<T> {
  const el = document.createElement(tag) as T;
  (el as unknown as { hass: unknown }).hass = api.hass;
  document.body.append(el);
  await settle(el);
  return el;
}

const menu = (el: HTMLElement) =>
  el.shadowRoot?.querySelector("ef-download-menu") as HTMLElement & {
    updateComplete: Promise<unknown>;
  };

const part = (el: HTMLElement) => menu(el).shadowRoot as ShadowRoot;

async function choose(
  el: HTMLElement & { updateComplete: Promise<unknown> },
  index: number,
) {
  const root = part(el);
  root.querySelector<HTMLButtonElement>("button.trigger")?.click();
  await menu(el).updateComplete;
  root.querySelectorAll<HTMLButtonElement>("[role=menuitem]")[index]?.click();
  await settle(el);
}

describe("audit download", () => {
  const page = (items: ReturnType<typeof change>[], next: number | null) => ({
    items,
    next_before_id: next,
  });

  it("pages through changes with the current filters, unmasked", async () => {
    const api = fakeApi({
      changes: (m: Record<string, unknown>) =>
        m.before_id === undefined
          ? page([change({ id: 9, actor_ip: "203.0.113.9" })], 9)
          : m.before_id === 9
            ? page(
                [change({ id: 8, actor_name: "=cmd", actor_ip: null })],
                null,
              )
            : page([], null),
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    setMaskIps(true);
    const before = api.of("changes").length;
    await choose(el, 0);
    const sent = api.of("changes").slice(before);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatchObject({ include_system: false, limit: 200 });
    expect(sent[0]).not.toHaveProperty("before_id");
    expect(sent[1]).toMatchObject({ before_id: 9, limit: 200 });
    expect(saved.calls).toHaveLength(1);
    const [file] = saved.calls;
    expect(file?.name).toMatch(
      /^electric-fence-audit-\d{4}-\d{2}-\d{2}-unmasked\.csv$/,
    );
    expect(file?.mime).toContain("text/csv");
    expect(file?.body.startsWith(BOM)).toBe(true);
    expect(file?.body).toContain("203.0.113.9");
    expect(file?.body).toContain("'=cmd");
    expect(file?.body.split("\r\n")[0]).toBe(
      BOM + "time,kind,action,target,target_name,actor,actor_ip,changed",
    );
  });

  it("adds a values column only when rows have values", async () => {
    const api = fakeApi({
      changes: page(
        [change({ id: 2, details: { changed: ["a", "b"], values: { a: 1 } } })],
        null,
      ),
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    await choose(el, 0);
    const lines = saved.calls[0]?.body.split("\r\n") ?? [];
    expect(lines[0]).toMatch(/,changed,values$/);
    expect(lines[1]).toContain('a; b,"{""a"":1}"');
  });

  it("exports JSON with the filters and the rows unchanged", async () => {
    const rows = [change({ id: 9, details: { changed: ["a"] } })];
    const api = fakeApi({
      changes: page(rows, null),
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    await choose(el, 1);
    const [file] = saved.calls;
    expect(file?.name).toMatch(/-unmasked\.json$/);
    const data = JSON.parse(file?.body ?? "{}") as Record<string, unknown>;
    expect(Object.keys(data)).toEqual([
      "exported_at",
      "filters",
      "truncated",
      "items",
    ]);
    expect(data.truncated).toBe(false);
    expect(data.items).toEqual(rows);
    expect(data.filters).toMatchObject({ include_system: false });
  });

  it("shows an error and downloads nothing when a page fails", async () => {
    let n = 0;
    const api = fakeApi({
      changes: () => {
        n += 1;
        if (n > 1) {
          throw new Error("boom");
        }
        return page([change({ id: 3 })], 3);
      },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    await choose(el, 0);
    expect(saved.calls).toHaveLength(0);
    expect(el.shadowRoot?.querySelector(".notice.error")?.textContent).toMatch(
      /download/i,
    );
  });
});

describe("activity download", () => {
  it("exports logins with the activity columns", async () => {
    const api = fakeApi({
      logins: {
        items: [
          login({
            id: 4,
            username_typed: '=HYPERLINK("x")',
            ip: "203.0.113.7",
            hostname: "h.example",
          }),
        ],
        next_before_id: null,
      },
      users: { items: [] },
    });
    const el = await mount<EfActivity>("ef-activity", api);
    await choose(el, 0);
    const [file] = saved.calls;
    expect(file?.name).toMatch(/^electric-fence-activity-.*-unmasked\.csv$/);
    const lines = file?.body.split("\r\n") ?? [];
    expect(lines[0]).toBe(
      BOM +
        "time,result,reason,username,user_exists,user,ip,country,hostname,source,client,user_agent,mfa_module,new_country,count",
    );
    expect(lines[1]).toContain(`"'=HYPERLINK(""x"")"`);
    expect(lines[1]).toContain("203.0.113.7");
    expect(lines[1]).toContain("h.example");
  });
});

describe("download menu", () => {
  it("opens on click, closes on Escape and on outside click", async () => {
    const api = fakeApi({
      changes: { items: [], next_before_id: null },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    const root = part(el);
    const trigger = root.querySelector<HTMLButtonElement>("button.trigger");
    expect(trigger?.getAttribute("aria-haspopup")).toBe("menu");
    expect(root.querySelector("[role=menu]")).toBeNull();
    trigger?.click();
    await menu(el).updateComplete;
    expect(trigger?.getAttribute("aria-expanded")).toBe("true");
    expect(root.querySelectorAll("[role=menuitem]")).toHaveLength(2);
    root.querySelector("[role=menu]")?.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Escape",
        bubbles: true,
        composed: true,
      }),
    );
    await menu(el).updateComplete;
    expect(root.querySelector("[role=menu]")).toBeNull();
    trigger?.click();
    await menu(el).updateComplete;
    document.body.click();
    await menu(el).updateComplete;
    expect(root.querySelector("[role=menu]")).toBeNull();
  });
});

describe("download while the view changes", () => {
  const gated = () => {
    const release: (() => void)[] = [];
    const api = fakeApi({
      changes: (m: Record<string, unknown>) => {
        if (m.limit !== 200) {
          return { items: [], next_before_id: null };
        }
        const before = (m.before_id as number | undefined) ?? 100;
        return new Promise((resolve) => {
          release.push(() =>
            resolve({
              items: [change({ id: before - 1 })],
              next_before_id: before - 1,
            }),
          );
        });
      },
      users: { items: [] },
    });
    return { api, release };
  };

  const exportCalls = (api: FakeApi) =>
    api.of("changes").filter((m) => m.limit === 200);

  it("shows progress and disables the button while collecting", async () => {
    const { api, release } = gated();
    const el = await mount<EfAudit>("ef-audit", api);
    const m = menu(el) as ReturnType<typeof menu> & { busy: boolean };
    const done = choose(el, 0);
    await settle(el);
    expect(m.busy).toBe(true);
    const trigger = part(el).querySelector<HTMLButtonElement>("button.trigger");
    expect(trigger?.disabled).toBe(true);
    expect(trigger?.textContent).toContain("Preparing… 0 rows");
    release.shift()?.();
    await settle(el);
    expect(trigger?.textContent).toContain("Preparing… 1 rows");
    void done;
  });

  it("stops paging and downloads nothing after disconnect", async () => {
    const { api, release } = gated();
    const el = await mount<EfAudit>("ef-audit", api);
    void choose(el, 0);
    await settle(el);
    expect(exportCalls(api)).toHaveLength(1);
    el.remove();
    release.shift()?.();
    await settle();
    expect(exportCalls(api)).toHaveLength(1);
    expect(saved.calls).toHaveLength(0);
    expect(el.shadowRoot?.querySelector(".notice.error")).toBeNull();
  });

  it("keeps the filter it started with when filters change", async () => {
    const { api, release } = gated();
    const el = await mount<EfAudit>("ef-audit", api);
    void choose(el, 0);
    await settle(el);
    const pane = el.shadowRoot?.querySelector("ef-filter-pane");
    pane?.dispatchEvent(
      new CustomEvent("filter-changed", {
        detail: { name: "kind", value: "user" },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(el);
    release.shift()?.();
    await settle(el);
    const calls = exportCalls(api);
    expect(calls).toHaveLength(2);
    for (const call of calls) {
      expect(call).not.toHaveProperty("kind");
    }
  });

  it("tells the user when the cap cut the download short", async () => {
    let next = 1_000_000;
    const api = fakeApi({
      changes: (m: Record<string, unknown>) => {
        if (m.limit !== 200) {
          return { items: [], next_before_id: null };
        }
        const items = Array.from({ length: 200 }, () => change({ id: next }));
        next -= 200;
        return { items, next_before_id: next };
      },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    await choose(el, 1);
    for (let i = 0; i < 600; i += 1) {
      await Promise.resolve();
    }
    await settle(el);
    const data = JSON.parse(saved.calls[0]?.body ?? "{}") as {
      truncated: boolean;
      items: unknown[];
    };
    expect(data.truncated).toBe(true);
    expect(data.items).toHaveLength(100_000);
    expect(el.shadowRoot?.querySelector(".notice")?.textContent).toContain(
      "100000",
    );
  });
});

describe("download menu keyboard", () => {
  it("closes on Escape at the host and returns focus after busy", async () => {
    const api = fakeApi({
      changes: { items: [], next_before_id: null },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    const m = menu(el) as ReturnType<typeof menu> & { busy: boolean };
    const root = part(el);
    root.querySelector<HTMLButtonElement>("button.trigger")?.click();
    await m.updateComplete;
    m.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await m.updateComplete;
    expect(root.querySelector("[role=menu]")).toBeNull();
    m.busy = true;
    await m.updateComplete;
    m.busy = false;
    await m.updateComplete;
    expect(root.activeElement?.classList.contains("trigger")).toBe(true);
  });

  it("closes when focus moves outside the component", async () => {
    const api = fakeApi({
      changes: { items: [], next_before_id: null },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", api);
    const m = menu(el);
    const root = part(el);
    root.querySelector<HTMLButtonElement>("button.trigger")?.click();
    await m.updateComplete;
    const other = document.createElement("button");
    document.body.append(other);
    m.dispatchEvent(new FocusEvent("focusout", { relatedTarget: other }));
    await m.updateComplete;
    expect(root.querySelector("[role=menu]")).toBeNull();
  });
});
