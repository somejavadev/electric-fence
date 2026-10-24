import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EfLoginRow } from "../src/components/ef-login-row.ts";
import {
  EfActivity,
  filtersFromQuery,
  matchesLogin,
} from "../src/views/ef-activity.ts";
import type { ActivityFilters } from "../src/views/ef-activity.ts";
import { EfAudit, matchesChange } from "../src/views/ef-audit.ts";
import type { AuditFilters } from "../src/views/ef-audit.ts";
import { EfSettings } from "../src/views/ef-settings.ts";
import { EfUsers } from "../src/views/ef-users.ts";
import { LIVE_CAP, REFRESH_DELAY_MS } from "../src/views/live-view.ts";
import type { LoginItem, Status, UserItem } from "../src/types.ts";
import { change, fakeApi, login, status } from "./helpers.ts";
import type { FakeApi } from "./helpers.ts";

async function settle(el?: { updateComplete: Promise<unknown> }) {
  for (let i = 0; i < 8; i += 1) {
    await Promise.resolve();
  }
  await el?.updateComplete;
  for (let i = 0; i < 4; i += 1) {
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

const user = (over: Partial<UserItem> = {}): UserItem => ({
  id: "u1",
  name: "Anna",
  username: "anna",
  is_admin: true,
  is_owner: false,
  is_active: true,
  local_only: false,
  system_generated: false,
  mfa_modules: [],
  last_login: login({ result: "success", reason: "ok" }),
  sessions: [
    {
      id: "s1",
      client_name: "Firefox",
      client_id: null,
      token_type: "normal",
      created_at: "2026-10-20T10:00:00+00:00",
      last_used_at: null,
      last_used_ip: "192.168.1.5",
    },
  ],
  ...over,
});

const select = (root: ShadowRoot, name: string) =>
  root.querySelector<HTMLSelectElement>(`[name=${name}]`) as HTMLSelectElement;

function bar(el: HTMLElement): ShadowRoot {
  return el.shadowRoot?.querySelector("ef-filter-bar")
    ?.shadowRoot as ShadowRoot;
}

function chooseOption(root: ShadowRoot, name: string, value: string) {
  const control = select(root, name);
  control.value = value;
  control.dispatchEvent(new Event("change", { bubbles: true }));
}

beforeEach(() => {
  history.replaceState(null, "", "/electric-fence/activity");
});

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("ef-activity", () => {
  const api = (extra: Record<string, unknown> = {}) =>
    fakeApi({
      logins: { items: [login({ id: 5 })], next_before_id: 5 },
      users: { items: [user()] },
      ...extra,
    });

  it("reads the URL query on load", async () => {
    history.replaceState(null, "", "/electric-fence/activity?result=failed");
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    expect(fake.of("logins")[0]).toMatchObject({ result: "failed" });
    expect(select(bar(el), "result").value).toBe("failed");
  });

  it("offers failed or blocked together", async () => {
    history.replaceState(
      null,
      "",
      "/electric-fence/activity?result=failed_or_blocked&range=24h",
    );
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    expect(fake.of("logins")[0]).toMatchObject({ result: "failed_or_blocked" });
    const control = select(bar(el), "result");
    expect(control.value).toBe("failed_or_blocked");
    expect(control.selectedOptions[0]?.textContent.trim()).toBe(
      "Failed or blocked",
    );
    const filters = filtersFromQuery("?result=failed_or_blocked");
    expect(filters.result).toBe("failed_or_blocked");
    const now = Date.parse("2026-10-24T10:00:00Z");
    for (const [result, expected] of [
      ["failed", true],
      ["blocked", true],
      ["success", false],
    ] as const) {
      expect(matchesLogin(login({ result }), filters, now)).toBe(expected);
    }
  });

  it("shows request details on its rows", async () => {
    const el = await mount<EfActivity>("ef-activity", api());
    const row = el.shadowRoot?.querySelector("ef-login-row");
    expect(row?.detailed).toBe(true);
    expect(row?.shadowRoot?.querySelector("button.toggle")).not.toBeNull();
  });

  it("ignores bad query values", () => {
    expect(filtersFromQuery("?result=nope&range=1y&country=ru")).toMatchObject({
      result: "",
      range: "24h",
      country: "RU",
    });
  });

  it("writes filter changes to the URL without adding history", async () => {
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    const before = history.length;
    chooseOption(bar(el), "result", "blocked");
    await settle(el);
    expect(location.search).toBe("?result=blocked");
    chooseOption(bar(el), "range", "all");
    await settle(el);
    expect(new URLSearchParams(location.search).get("range")).toBe("all");
    expect(history.length).toBe(before);
    const last = fake.of("logins").at(-1);
    expect(last).toMatchObject({ result: "blocked" });
    expect(last).not.toHaveProperty("since");
  });

  it("maps the time range to an ISO UTC since", async () => {
    vi.setSystemTime(new Date("2026-10-24T12:00:00Z"));
    const fake = api();
    await mount<EfActivity>("ef-activity", fake);
    expect(fake.of("logins")[0]?.["since"]).toBe("2026-10-23T12:00:00.000Z");
  });

  it("loads more with before_id", async () => {
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    const more = [...(el.shadowRoot?.querySelectorAll("button") ?? [])].find(
      (b) => b.textContent.trim() === "Load more",
    );
    more?.click();
    await settle(el);
    expect(fake.of("logins").at(-1)).toMatchObject({ before_id: 5 });
  });

  it("prepends matching pushes, never uses them as cursors", async () => {
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    history.replaceState(null, "", "/electric-fence/activity?result=failed");
    const filtered = await mount<EfActivity>("ef-activity", fake);
    const rows = () =>
      filtered.shadowRoot?.querySelectorAll("ef-login-row").length;
    expect(rows()).toBe(1);
    fake.push({
      type: "login",
      item: login({ id: null, ts: new Date().toISOString() }),
    });
    fake.push({
      type: "login",
      item: login({
        id: null,
        result: "success",
        ts: new Date().toISOString(),
      }),
    });
    await settle(filtered);
    expect(rows()).toBe(2);
    expect(el.shadowRoot?.querySelectorAll("ef-login-row").length).toBe(1);
    [...(filtered.shadowRoot?.querySelectorAll("button") ?? [])]
      .find((b) => b.textContent.trim() === "Load more")
      ?.click();
    await settle(filtered);
    expect(fake.of("logins").at(-1)).toMatchObject({ before_id: 5 });
  });

  it("caps live items at 200", async () => {
    const fake = api({ logins: { items: [], next_before_id: null } });
    const el = await mount<EfActivity>("ef-activity", fake);
    for (let i = 0; i < 230; i += 1) {
      fake.push({
        type: "login",
        item: login({ id: null, ts: new Date().toISOString() }),
      });
    }
    await settle(el);
    expect(el.shadowRoot?.querySelectorAll("ef-login-row").length).toBe(200);
  });

  it("stays unloaded after an unloaded event", async () => {
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    const calls = fake.sent.length;
    fake.push({ type: "unloaded" });
    await settle(el);
    el.hass = fake.hass;
    await settle(el);
    expect(fake.sent.length).toBe(calls);
    expect(el.shadowRoot?.textContent).toContain("unloaded");
  });

  it("has labelled filters", async () => {
    const el = await mount<EfActivity>("ef-activity", api());
    const labels = [...bar(el).querySelectorAll("label")].map((l) =>
      l.textContent.trim().split("\n")[0]?.trim(),
    );
    expect(labels).toEqual([
      "Result",
      "User",
      "Typed username",
      "IP address",
      "Country code",
      "Time range",
    ]);
  });
});

describe("ef-audit", () => {
  const api = () =>
    fakeApi({
      changes: {
        items: [
          change({ id: 3, details: { keys: ["latitude"] } }),
          change({ id: 2, actor_user_id: null, actor_name: null }),
        ],
        next_before_id: null,
      },
      users: { items: [user()] },
    });

  it("hides system changes by default and refetches when toggled", async () => {
    const fake = api();
    const el = await mount<EfAudit>("ef-audit", fake);
    expect(fake.of("changes")[0]).toMatchObject({ include_system: false });
    const box = bar(el).querySelector<HTMLInputElement>("[name=system]");
    expect(box?.checked).toBe(false);
    (box as HTMLInputElement).checked = true;
    box?.dispatchEvent(new Event("change", { bubbles: true }));
    await settle(el);
    expect(fake.of("changes").at(-1)).toMatchObject({ include_system: true });
  });

  it("shows System as the actor and the changed key names", async () => {
    const el = await mount<EfAudit>("ef-audit", api());
    const rows = [...(el.shadowRoot?.querySelectorAll("ef-change-row") ?? [])];
    expect(rows[1]?.shadowRoot?.querySelector(".actor")?.textContent).toBe(
      "System",
    );
    expect(el.shadowRoot?.querySelector(".details bdi")?.textContent).toBe(
      "latitude",
    );
  });

  it("filters pushes by system flag", async () => {
    const fake = api();
    const el = await mount<EfAudit>("ef-audit", fake);
    fake.push({
      type: "change",
      item: change({
        id: null,
        actor_user_id: null,
        ts: new Date().toISOString(),
      }),
    });
    fake.push({
      type: "change",
      item: change({ id: null, ts: new Date().toISOString() }),
    });
    await settle(el);
    expect(el.shadowRoot?.querySelectorAll("ef-change-row").length).toBe(3);
  });
});

describe("ef-users", () => {
  it("renders sessions on expand and has only expand buttons", async () => {
    const fake = fakeApi({
      users: { items: [user(), user({ id: "u2", name: "Bo", sessions: [] })] },
    });
    const el = await mount<EfUsers>("ef-users", fake);
    const root = el.shadowRoot as ShadowRoot;
    expect(root.textContent).not.toContain("Firefox");
    const buttons = [...root.querySelectorAll("button")];
    expect(buttons).toHaveLength(2);
    for (const b of buttons) {
      expect(b.getAttribute("aria-expanded")).toBe("false");
    }
    buttons[0]?.click();
    await settle(el);
    expect(root.textContent).toContain("Firefox");
    expect(buttons[0]?.getAttribute("aria-expanded")).toBe("true");
    for (const b of root.querySelectorAll("button")) {
      expect(b.hasAttribute("aria-expanded")).toBe(true);
    }
    expect(root.querySelectorAll("th[scope]").length).toBeGreaterThan(5);
    expect(root.textContent).toContain("no two-factor");
  });

  it("refetches throttled on a login push", async () => {
    const fake = fakeApi({ users: { items: [user()] } });
    await mount<EfUsers>("ef-users", fake);
    vi.useFakeTimers();
    for (let i = 0; i < 5; i += 1) {
      fake.push({ type: "login", item: login({ id: null }) });
      await vi.advanceTimersByTimeAsync(100);
    }
    expect(fake.of("users")).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(REFRESH_DELAY_MS);
    expect(fake.of("users")).toHaveLength(2);
  });
});

describe("ef-settings", () => {
  const withStatus = (s: Status) => fakeApi({ status: s });

  it("shows the database date as a date, without a time zone shift", async () => {
    const s = status();
    s.geo = {
      provider: "dbip_lite",
      db_date: "2026-10-01",
      last_error: null,
      attribution: null,
    };
    const fake = withStatus(s);
    (fake.hass as { config: { time_zone: string } }).config.time_zone =
      "Pacific/Honolulu";
    const el = await mount<EfSettings>("ef-settings", fake);
    const text = el.shadowRoot?.textContent ?? "";
    expect(text).toMatch(/Database date\s*Oct 1, 2026\s/);
    expect(text).not.toContain("Sep 30");
    expect(text).not.toMatch(/\d:\d\d/);
  });

  it("shows the attribution when present", async () => {
    const el = await mount<EfSettings>(
      "ef-settings",
      withStatus(status("IP data by DB-IP")),
    );
    expect(el.shadowRoot?.querySelector(".attribution")?.textContent).toContain(
      "IP data by DB-IP",
    );
  });

  it("omits the attribution when absent", async () => {
    const el = await mount<EfSettings>("ef-settings", withStatus(status(null)));
    expect(el.shadowRoot?.querySelector(".attribution")).toBeNull();
  });

  it("shows collectors, reasons and counters", async () => {
    const s = status();
    s.collectors = [{ name: "login", enabled: false, reason: ["no hook"] }];
    s.worker = { queued: 4, dropped: 5, storage_errors: 6 };
    const el = await mount<EfSettings>("ef-settings", withStatus(s));
    const text = el.shadowRoot?.textContent ?? "";
    expect(text).toContain("no hook");
    expect(text).toMatch(/Queued\s*4/);
    expect(text).toMatch(/Dropped\s*5/);
    expect(text).toMatch(/Storage errors\s*6/);
  });

  it("opens the integration settings", async () => {
    const seen: unknown[] = [];
    window.addEventListener("location-changed", (e) => seen.push(e), {
      once: true,
    });
    const el = await mount<EfSettings>("ef-settings", withStatus(status()));
    const button = el.shadowRoot?.querySelector("button");
    expect(button?.textContent.trim()).toBe("Open settings");
    button?.click();
    expect(location.pathname).toBe(
      "/config/integrations/integration/electric_fence",
    );
    expect(seen).toHaveLength(1);
  });
});

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const loadMoreButton = (el: HTMLElement) =>
  [...(el.shadowRoot?.querySelectorAll("button") ?? [])].find(
    (b) => b.textContent.trim() === "Load more",
  );

const rowCount = (el: HTMLElement, tag: string) =>
  el.shadowRoot?.querySelectorAll(tag).length;

describe("audit details use the real backend shapes", () => {
  const render = async (details: Record<string, unknown>) => {
    const fake = fakeApi({
      changes: { items: [change({ details })], next_before_id: null },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", fake);
    return el.shadowRoot?.querySelector(".details")?.textContent ?? null;
  };
  const squash = (t: string | null) => t?.replace(/\s+/g, " ").trim();

  it("config entry", async () => {
    const text = squash(
      await render({
        domain: "tuya",
        changed: ["options", "title"],
        data_keys_changed: ["token"],
        options_keys_changed: ["scan_interval"],
      }),
    );
    expect(text).toBe(
      "changed: options, title · data keys: token · option keys: scan_interval",
    );
  });

  it("user update, username change and core config", async () => {
    expect(squash(await render({ changed: ["name"] }))).toBe("changed: name");
    expect(squash(await render({ field: "username" }))).toBe("field: username");
    expect(squash(await render({ keys: ["latitude"] }))).toBe("keys: latitude");
  });

  it("shows nothing for empty or unknown details", async () => {
    expect(await render({})).toBeNull();
    expect(await render({ changed: [], domain: "tuya" })).toBeNull();
    expect(await render({ secret: "x" })).toBeNull();
  });
});

describe("a Load more in flight across a filter change", () => {
  const cases = [
    {
      name: "activity",
      tag: "ef-activity",
      command: "logins",
      row: "ef-login-row",
      make: (id: number | null) => login({ id }),
      filter: ["result", "blocked"],
    },
    {
      name: "audit",
      tag: "ef-audit",
      command: "changes",
      row: "ef-change-row",
      make: (id: number | null) => change({ id }),
      filter: ["kind", "user"],
    },
  ] as const;

  const setup = async (c: (typeof cases)[number]) => {
    const pending: ReturnType<typeof deferred<unknown>>[] = [];
    let n = 0;
    const fake = fakeApi({
      users: { items: [] },
      [c.command]: () => {
        n += 1;
        if (n === 1) {
          return { items: [c.make(9)], next_before_id: 9 };
        }
        const d = deferred<unknown>();
        pending.push(d);
        return d.promise;
      },
    });
    const el = await mount<HTMLElement & { updateComplete: Promise<unknown> }>(
      c.tag,
      fake,
    );
    return { fake, el, pending };
  };

  for (const c of cases) {
    for (const order of ["stale-first", "refetch-first"] as const) {
      it(`${c.name}: ${order} never mixes lists`, async () => {
        const { fake, el, pending } = await setup(c);
        loadMoreButton(el)?.click(); // A: Load more, in flight
        await settle(el);
        chooseOption(bar(el), c.filter[0], c.filter[1]);
        await settle(el);
        const [stale, refetch] = pending as [
          ReturnType<typeof deferred<unknown>>,
          ReturnType<typeof deferred<unknown>>,
        ];
        expect(fake.of(c.command).at(-2)).toHaveProperty("before_id", 9);
        const staleAnswer = {
          items: [c.make(1), c.make(2)],
          next_before_id: 1,
        };
        const freshAnswer = { items: [c.make(4)], next_before_id: null };
        if (order === "stale-first") {
          stale.resolve(staleAnswer);
          await settle(el);
          refetch.resolve(freshAnswer);
        } else {
          refetch.resolve(freshAnswer);
          await settle(el);
          stale.resolve(staleAnswer);
        }
        await settle(el);
        expect(rowCount(el, c.row)).toBe(1);
        expect(loadMoreButton(el)).toBeUndefined();
      });
    }

    it(`${c.name}: the new list's Load more is not blocked, and a late stale one cannot unblock a newer one`, async () => {
      const { fake, el, pending } = await setup(c);
      loadMoreButton(el)?.click(); // A
      await settle(el);
      chooseOption(bar(el), c.filter[0], c.filter[1]);
      await settle(el);
      const [stale, refetch] = pending as [
        ReturnType<typeof deferred<unknown>>,
        ReturnType<typeof deferred<unknown>>,
      ];
      refetch.resolve({ items: [c.make(4)], next_before_id: 4 });
      await settle(el);
      const button = loadMoreButton(el);
      expect(button?.disabled).toBe(false);
      button?.click(); // B, on the new list
      await settle(el);
      const before = fake.of(c.command).length;
      expect(fake.of(c.command).at(-1)).toHaveProperty("before_id", 4);
      stale.resolve({ items: [c.make(1)], next_before_id: 1 });
      await settle(el);
      // B is still in flight: the button must stay disabled.
      expect(loadMoreButton(el)?.disabled).toBe(true);
      loadMoreButton(el)?.click();
      await settle(el);
      expect(fake.of(c.command)).toHaveLength(before);
      expect(rowCount(el, c.row)).toBe(1);
    });
  }
});

describe("live cap", () => {
  it("renders 200 of 201 pushed items", async () => {
    const fake = fakeApi({
      users: { items: [] },
      logins: { items: [], next_before_id: null },
      changes: { items: [], next_before_id: null },
    });
    const a = await mount<EfActivity>("ef-activity", fake);
    for (let i = 0; i < LIVE_CAP + 1; i += 1) {
      fake.push({
        type: "login",
        item: login({ id: null, ts: new Date().toISOString() }),
      });
    }
    await settle(a);
    expect(rowCount(a, "ef-login-row")).toBe(200);
    a.remove();
    const u = await mount<EfAudit>("ef-audit", fake);
    for (let i = 0; i < LIVE_CAP + 1; i += 1) {
      fake.push({
        type: "change",
        item: change({ id: null, ts: new Date().toISOString() }),
      });
    }
    await settle(u);
    expect(rowCount(u, "ef-change-row")).toBe(200);
  });
});

describe("push filter semantics", () => {
  const NOW = Date.parse("2026-10-24T12:00:00Z");
  const base: ActivityFilters = {
    result: "",
    user: "",
    username: "",
    ip: "",
    country: "",
    range: "24h",
  };
  const fresh = login({ ts: "2026-10-24T11:00:00Z" });
  const m = (over: Partial<ActivityFilters>, item = fresh) =>
    matchesLogin(item, { ...base, ...over }, NOW);

  it("matchesLogin", () => {
    expect(m({})).toBe(true);
    expect(m({ result: "failed" })).toBe(true);
    expect(m({ result: "success" })).toBe(false);
    expect(m({ username: "ADM" })).toBe(true);
    expect(m({ username: "dmi" })).toBe(true);
    expect(m({ username: "zzz" })).toBe(false);
    expect(
      m({ username: "x" }, login({ username_typed: null, ts: fresh.ts })),
    ).toBe(false);
    expect(m({ ip: "203.0.113.7" })).toBe(true);
    expect(m({ ip: "203.0.113" })).toBe(false);
    expect(m({ country: "RU" })).toBe(true);
    expect(m({ country: "R" })).toBe(false);
    expect(m({ user: "u1" })).toBe(false);
    expect(m({ user: "u1" }, login({ user_id: "u1", ts: fresh.ts }))).toBe(
      true,
    );
    expect(m({}, login({ ts: "2026-10-22T11:00:00Z" }))).toBe(false);
    expect(m({ range: "7d" }, login({ ts: "2026-10-22T11:00:00Z" }))).toBe(
      true,
    );
    expect(m({ range: "all" }, login({ ts: "2020-01-01T00:00:00Z" }))).toBe(
      true,
    );
  });

  it("username folds ASCII only, like SQLite LIKE", () => {
    const item = login({ username_typed: "ÄDMIN", ts: fresh.ts });
    expect(m({ username: "ädmin" }, item)).toBe(false);
    expect(m({ username: "dmin" }, item)).toBe(true);
  });

  it("matchesChange", () => {
    const f: AuditFilters = { kind: "", actor: "", range: "7d", system: false };
    const c = (
      over: Partial<AuditFilters>,
      item = change({ ts: "2026-10-24T11:00:00Z" }),
    ) => matchesChange(item, { ...f, ...over }, NOW);
    expect(c({})).toBe(true);
    expect(c({ kind: "user" })).toBe(false);
    expect(c({ kind: "config_entry" })).toBe(true);
    expect(c({ actor: "u2" })).toBe(false);
    expect(c({ actor: "u1" })).toBe(true);
    const sys = change({ actor_user_id: null, ts: "2026-10-24T11:00:00Z" });
    expect(c({}, sys)).toBe(false);
    expect(c({ system: true }, sys)).toBe(true);
    const old = change({ ts: "2026-10-01T00:00:00Z" });
    expect(c({}, old)).toBe(false);
    expect(c({ range: "all" }, old)).toBe(true);
  });
});

describe("paging and lifecycle", () => {
  it("Load more appends and hides the button at the end", async () => {
    let n = 0;
    const fake = fakeApi({
      users: { items: [] },
      logins: () => {
        n += 1;
        return n === 1
          ? { items: [login({ id: 9 })], next_before_id: 9 }
          : { items: [login({ id: 8 })], next_before_id: null };
      },
    });
    const el = await mount<EfActivity>("ef-activity", fake);
    expect(rowCount(el, "ef-login-row")).toBe(1);
    loadMoreButton(el)?.click();
    await settle(el);
    expect(rowCount(el, "ef-login-row")).toBe(2);
    expect(loadMoreButton(el)).toBeUndefined();
  });

  it("a Load more failure shows an error and keeps the list", async () => {
    let n = 0;
    const fake = fakeApi({
      users: { items: [] },
      logins: () => {
        n += 1;
        if (n === 1) {
          return { items: [login({ id: 9 })], next_before_id: 9 };
        }
        throw new Error("boom");
      },
    });
    const el = await mount<EfActivity>("ef-activity", fake);
    loadMoreButton(el)?.click();
    await settle(el);
    expect(el.shadowRoot?.querySelector("[role=alert]")).not.toBeNull();
    expect(rowCount(el, "ef-login-row")).toBe(1);
  });

  it("unsubscribes on disconnect", async () => {
    let unsubs = 0;
    const fake = fakeApi({
      users: { items: [] },
      logins: { items: [], next_before_id: null },
    });
    (
      fake.hass.connection as unknown as { subscribeMessage: unknown }
    ).subscribeMessage = () => Promise.resolve(() => (unsubs += 1));
    const el = await mount<EfActivity>("ef-activity", fake);
    el.remove();
    expect(unsubs).toBe(1);
  });

  it("retry after unload restarts; so does a visible tab", async () => {
    const fake = fakeApi({
      users: { items: [] },
      logins: { items: [], next_before_id: null },
    });
    const el = await mount<EfActivity>("ef-activity", fake);
    fake.push({ type: "unloaded" });
    await settle(el);
    const before = fake.of("logins").length;
    el.shadowRoot?.querySelector<HTMLButtonElement>(".notice button")?.click();
    await settle(el);
    expect(fake.of("logins").length).toBe(before + 1);
    expect(el.shadowRoot?.textContent).not.toContain("unloaded");

    fake.push({ type: "unloaded" });
    await settle(el);
    expect(el.shadowRoot?.textContent).toContain("unloaded");
    document.dispatchEvent(new Event("visibilitychange"));
    await settle(el);
    expect(fake.of("logins").length).toBe(before + 2);
  });

  it("prepends exactly the matching pushes, and caps at LIVE_CAP", async () => {
    history.replaceState(null, "", "/electric-fence/activity?result=failed");
    const fake = fakeApi({
      users: { items: [] },
      logins: { items: [login({ id: 5 })], next_before_id: null },
    });
    const el = await mount<EfActivity>("ef-activity", fake);
    expect(rowCount(el, "ef-login-row")).toBe(1);
    const now = new Date().toISOString();
    fake.push({
      type: "login",
      item: login({ id: null, ts: now, username_typed: "pushed" }),
    });
    fake.push({
      type: "login",
      item: login({ id: null, ts: now, result: "success" }),
    });
    await settle(el);
    const rows = [...(el.shadowRoot?.querySelectorAll("ef-login-row") ?? [])];
    expect(rows).toHaveLength(2);
    expect(
      (rows[0] as unknown as { item: { username_typed: string } }).item
        .username_typed,
    ).toBe("pushed");
    expect(LIVE_CAP).toBe(200);
  });
});

describe("users accessibility", () => {
  it("hides glyphs and sets aria-controls only when expanded", async () => {
    const el = await mount<EfUsers>(
      "ef-users",
      fakeApi({ users: { items: [user()] } }),
    );
    const root = el.shadowRoot as ShadowRoot;
    const button = root.querySelector("button") as HTMLButtonElement;
    expect(button.hasAttribute("aria-controls")).toBe(false);
    expect(button.querySelector('[aria-hidden="true"]')).not.toBeNull();
    button.click();
    await settle(el);
    const controls = button.getAttribute("aria-controls");
    expect(controls).not.toBeNull();
    expect(
      root.getElementById?.(controls ?? "") ??
        root.querySelector(`[id="${controls}"]`),
    ).not.toBeNull();
  });
});

describe("Load more flag across a restart", () => {
  const cases = [
    {
      tag: "ef-activity",
      command: "logins",
      make: (id: number) => login({ id }),
    },
    {
      tag: "ef-audit",
      command: "changes",
      make: (id: number) => change({ id }),
    },
  ] as const;

  for (const c of cases) {
    for (const how of ["unloaded then retry", "disconnect then reconnect"]) {
      it(`${c.tag}: ${how} leaves Load more enabled`, async () => {
        const late = deferred<unknown>();
        let calls = 0;
        const fake = fakeApi({
          users: { items: [] },
          [c.command]: (m: Record<string, unknown>) => {
            calls += 1;
            return m["before_id"] !== undefined && calls === 2
              ? late.promise
              : { items: [c.make(9)], next_before_id: 9 };
          },
        });
        const el = await mount<
          HTMLElement & { updateComplete: Promise<unknown> }
        >(c.tag, fake);
        loadMoreButton(el)?.click();
        await settle(el);
        if (how === "unloaded then retry") {
          fake.push({ type: "unloaded" });
          await settle(el);
          late.resolve({ items: [c.make(1)], next_before_id: 1 });
          await settle(el);
          el.shadowRoot
            ?.querySelector<HTMLButtonElement>(".notice button")
            ?.click();
        } else {
          el.remove();
          late.resolve({ items: [c.make(1)], next_before_id: 1 });
          await settle();
          document.body.append(el);
        }
        await settle(el);
        expect(loadMoreButton(el)?.disabled).toBe(false);
      });
    }
  }
});

describe("ef-login-row", () => {
  const hass = fakeApi({}).hass;

  async function row(
    item: Partial<LoginItem>,
    variant: "failed" | "success" = "failed",
    detailed = false,
  ): Promise<ShadowRoot> {
    const el = document.createElement("ef-login-row");
    el.hass = hass;
    el.variant = variant;
    el.detailed = detailed;
    el.item = login(item);
    document.body.append(el);
    await el.updateComplete;
    return el.shadowRoot as ShadowRoot;
  }

  const name = (root: ShadowRoot) =>
    root.querySelector(".username")?.textContent ?? "";

  it("names the user on success and blocked rows, the typed text on failures", async () => {
    const named = { username_typed: "typed", user_name: "Anna" };
    expect(name(await row({ ...named, result: "success" }, "success"))).toBe(
      "Anna",
    );
    expect(name(await row({ ...named, result: "blocked" }))).toBe("Anna");
    expect(name(await row({ ...named, result: "failed" }))).toBe("typed");
    // trusted_networks: no typed username at all.
    expect(
      name(
        await row(
          { result: "success", username_typed: null, user_name: "Anna" },
          "success",
        ),
      ),
    ).toBe("Anna");
    expect(
      name(await row({ result: "success", user_name: null }, "success")),
    ).toBe("admin");
    expect(
      name(
        await row(
          { result: "success", user_name: null, username_typed: null },
          "success",
        ),
      ),
    ).toBe("unknown user");
  });

  it("tags unconfirmed successes", async () => {
    const tags = async (confirmed: boolean) =>
      [
        ...(
          await row({ result: "success", confirmed }, "success")
        ).querySelectorAll(".tag"),
      ].map((t) => t.textContent.trim());
    expect(await tags(false)).toContain("unconfirmed");
    expect(await tags(true)).not.toContain("unconfirmed");
  });

  it("shows the URL of an invalid token, truncated", async () => {
    const url = `/api/${"x".repeat(100)}`;
    const root = await row({
      reason: "invalid_token",
      url,
      username_typed: null,
    });
    const shown = root.querySelector(".url");
    expect(shown?.getAttribute("title")).toBe(url);
    expect(shown?.textContent.length).toBe(65);
    expect((await row({ url })).querySelector(".url")).toBeNull();
  });

  it("expands a detail line with the request details", async () => {
    const root = await row(
      {
        url: "/auth/login_flow/abc",
        hostname: "host.example.net",
        user_agent: "Firefox/140",
        provider: "homeassistant",
        mfa_module: "totp",
        confirmed: true,
      },
      "failed",
      true,
    );
    const toggle = root.querySelector<HTMLButtonElement>("button.toggle");
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(root.querySelector(".details")).toBeNull();
    toggle?.click();
    await (root.host as EfLoginRow).updateComplete;
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    const details = root.querySelector(".details")?.textContent ?? "";
    for (const value of [
      "/auth/login_flow/abc",
      "host.example.net",
      "Firefox/140",
      "homeassistant",
      "totp",
      "confirmed",
    ]) {
      expect(details).toContain(value);
    }
    expect((await row({}, "failed", false)).querySelector("button")).toBeNull();
    const unconfirmed = await row({ confirmed: false }, "success", true);
    unconfirmed.querySelector<HTMLButtonElement>("button.toggle")?.click();
    await (unconfirmed.host as EfLoginRow).updateComplete;
    expect(unconfirmed.querySelector(".details")?.textContent).toContain(
      "unconfirmed",
    );
  });
});
