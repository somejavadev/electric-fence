import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EfLoginRow } from "../src/components/ef-login-row.ts";
import {
  EfActivity,
  filtersFromQuery,
  matchesLogin,
} from "../src/views/ef-activity.ts";
import type { ActivityFilters } from "../src/views/ef-activity.ts";
import {
  DEFAULT_AUDIT_FILTERS,
  EfAudit,
  matchesChange,
} from "../src/views/ef-audit.ts";
import type { AuditFilters } from "../src/views/ef-audit.ts";
import { EfSettings } from "../src/views/ef-settings.ts";
import { EfChangeRow } from "../src/components/ef-change-row.ts";
import { changeSentence } from "../src/strings.ts";
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
  const pane = el.shadowRoot?.querySelector("ef-filter-pane");
  if (pane !== null && pane !== undefined) {
    pane.open = true;
    await settle(pane);
  }
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
  return el.shadowRoot?.querySelector("ef-filter-pane")
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
    expect(history).toHaveLength(before);
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
    expect(el.shadowRoot?.querySelectorAll("ef-login-row")).toHaveLength(1);
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
    expect(el.shadowRoot?.querySelectorAll("ef-login-row")).toHaveLength(200);
  });

  it("stays unloaded after an unloaded event", async () => {
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    const calls = fake.sent.length;
    fake.push({ type: "unloaded" });
    await settle(el);
    el.hass = fake.hass;
    await settle(el);
    expect(fake.sent).toHaveLength(calls);
    expect(el.shadowRoot?.textContent).toContain("unloaded");
  });

  it("has labelled filters", async () => {
    const el = await mount<EfActivity>("ef-activity", api());
    const labels = [...bar(el).querySelectorAll("summary .label")].map((l) =>
      l.textContent.trim(),
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
    expect(el.shadowRoot?.querySelectorAll("ef-change-row")).toHaveLength(3);
  });
});

describe("ef-users", () => {
  it("shows sessions in a dialog and has no actions besides opening and closing", async () => {
    const fake = fakeApi({
      users: { items: [user(), user({ id: "u2", name: "Bo", sessions: [] })] },
    });
    const el = await mount<EfUsers>("ef-users", fake);
    const root = el.shadowRoot as ShadowRoot;
    expect(root.textContent).not.toContain("Firefox");
    const rows = [...root.querySelectorAll<HTMLButtonElement>("button.user")];
    expect(rows).toHaveLength(2);
    expect(root.querySelectorAll("button")).toHaveLength(2);
    expect(root.textContent).toContain("No two-factor");
    rows[0]?.click();
    await settle(el);
    const dialog = root.querySelector("dialog") as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(dialog.textContent).toContain("Firefox");
    expect(
      [...root.querySelectorAll("button")].map((b) => b.className),
    ).toEqual(["user", "user", "close"]);
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

  it("says app auditing is not available on this install type", async () => {
    const s = status();
    s.apps = "not_available";
    const el = await mount<EfSettings>("ef-settings", withStatus(s));
    expect(el.shadowRoot?.textContent).toContain(
      "Not available on this install type",
    );
  });

  it("says app auditing is turned off when Home Assistant changed", async () => {
    const s = status();
    s.apps = "disabled";
    const el = await mount<EfSettings>("ef-settings", withStatus(s));
    expect(el.shadowRoot?.textContent).toContain(
      "App auditing is turned off because Home Assistant changed (see Repairs)",
    );
  });

  it("shows forwarding state with queued and dropped counts", async () => {
    const s = status();
    s.settings.forward_enabled = true;
    s.forwarding = {
      state: "failing",
      queued: 12,
      dropped: 3,
      last_error: "OSError",
    };
    const el = await mount<EfSettings>("ef-settings", withStatus(s));
    const text = (el.shadowRoot?.textContent ?? "").replace(/\s+/g, " ");
    expect(text).toContain("Log forwarding");
    expect(text).toContain("Failing");
    expect(text).toMatch(/Queued 12/);
    expect(text).toMatch(/Dropped 3/);
    expect(text).toContain("OSError");
  });

  it("shows forwarding and integration checks as off", async () => {
    const s = status();
    s.settings.verify_files = false;
    const el = await mount<EfSettings>("ef-settings", withStatus(s));
    const text = (el.shadowRoot?.textContent ?? "").replace(/\s+/g, " ");
    expect(text).toMatch(/Forwarding Off/);
    expect(text).toMatch(/File checks Off/);
    expect(text).toMatch(/Repairs On/);
  });

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
    const button = [...(el.shadowRoot?.querySelectorAll("button") ?? [])].find(
      (b) => b.textContent.trim() === "Open settings",
    );
    expect(button).toBeDefined();
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
      "Changed: Options, Title Data keys: token Option keys: scan_interval",
    );
  });

  it("user update, username change and core config", async () => {
    expect(squash(await render({ changed: ["name"] }))).toBe("Changed: Name");
    expect(squash(await render({ field: "username" }))).toBe("field: username");
    expect(squash(await render({ keys: ["latitude"] }))).toBe("keys: latitude");
  });

  it("lists the new kinds in the filter", async () => {
    const fake = fakeApi({
      changes: { items: [], next_before_id: null },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", fake);
    const bar = el.shadowRoot?.querySelector("ef-filter-pane") as unknown as {
      fields: { name: string; options: { label: string }[] }[];
    };
    const kind = bar.fields.find((f) => f.name === "kind");
    expect(kind?.options.map((o) => o.label)).toEqual([
      "Any kind",
      "Integrations",
      "Users",
      "Core settings",
      "Backups",
      "HTTP",
      "Network",
      "Analytics",
      "Logging",
      "Recorder",
      "IP bans",
      "Login protection",
      "Apps",
      "Integration files",
      "Integration health",
      "Log forwarding",
    ]);
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
    from: "",
    to: "",
    search: "",
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

  it("matchesChange finds outside rows by their label", () => {
    const item = change({
      kind: "app",
      actor_user_id: null,
      actor_name: "outside",
      actor_ip: null,
    });
    const f = { ...DEFAULT_AUDIT_FILTERS, system: true };
    expect(matchesChange(item, { ...f, search: "outside home" }, NOW)).toBe(
      true,
    );
    expect(matchesChange(item, { ...f, search: "nothing" }, NOW)).toBe(false);
  });

  it("matchesChange", () => {
    const f: AuditFilters = { ...DEFAULT_AUDIT_FILTERS };
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
    const outside = change({
      actor_user_id: null,
      actor_name: "outside",
      actor_ip: null,
      ts: "2026-10-24T11:00:00Z",
    });
    expect(c({}, outside)).toBe(true);
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
    expect(fake.of("logins")).toHaveLength(before + 1);
    expect(el.shadowRoot?.textContent).not.toContain("unloaded");

    fake.push({ type: "unloaded" });
    await settle(el);
    expect(el.shadowRoot?.textContent).toContain("unloaded");
    document.dispatchEvent(new Event("visibilitychange"));
    await settle(el);
    expect(fake.of("logins")).toHaveLength(before + 2);
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
  it("hides the chevron and labels the dialog with the user's name", async () => {
    const el = await mount<EfUsers>(
      "ef-users",
      fakeApi({ users: { items: [user()] } }),
    );
    const root = el.shadowRoot as ShadowRoot;
    const button = root.querySelector("button.user") as HTMLButtonElement;
    expect(button.getAttribute("aria-haspopup")).toBe("dialog");
    expect(button.querySelector('ha-icon[aria-hidden="true"]')).not.toBeNull();
    button.click();
    await settle(el);
    const dialog = root.querySelector("dialog") as HTMLDialogElement;
    const title = root.querySelector(
      `[id="${dialog.getAttribute("aria-labelledby") ?? ""}"]`,
    );
    expect(title?.textContent).toContain("Anna");
    const close = dialog.querySelector("button.close");
    expect(close?.getAttribute("aria-label")).toBe("Close");
    expect(close?.querySelector('ha-icon[aria-hidden="true"]')).not.toBeNull();
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
    expect(await tags(false)).toContain("Unconfirmed");
    expect(await tags(true)).not.toContain("Unconfirmed");
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
    expect(shown?.textContent).toHaveLength(65);
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
      "Home Assistant",
      "totp",
      "Confirmed",
    ]) {
      expect(details).toContain(value);
    }
    expect((await row({}, "failed", false)).querySelector("button")).toBeNull();
    const unconfirmed = await row({ confirmed: false }, "success", true);
    unconfirmed.querySelector<HTMLButtonElement>("button.toggle")?.click();
    await (unconfirmed.host as EfLoginRow).updateComplete;
    expect(unconfirmed.querySelector(".details")?.textContent).toContain(
      "Unconfirmed",
    );
  });
});

describe("protection in Activity", () => {
  async function row(item: Partial<LoginItem>): Promise<ShadowRoot> {
    const el = document.createElement("ef-login-row");
    el.hass = fakeApi({}).hass;
    el.variant = item.result === "success" ? "success" : "failed";
    el.detailed = true;
    el.item = login(item);
    document.body.append(el);
    await el.updateComplete;
    return el.shadowRoot as ShadowRoot;
  }

  const tags = (root: ShadowRoot) =>
    [...root.querySelectorAll(".tag")].map((t) => t.textContent.trim());

  it("activity new country tag", async () => {
    const root = await row({
      result: "success",
      reason: "ok",
      confirmed: true,
      new_country: true,
      country_code: "BR",
    });
    expect(tags(root)).toContain("New country");
    root.querySelector<HTMLButtonElement>("button.toggle")?.click();
    await (root.host as EfLoginRow).updateComplete;
    const details = root.querySelector(".details") as HTMLElement;
    expect(details.textContent).toMatch(/Country\s*BR\s*New country/);
    expect(details.querySelector("ef-flag")).not.toBeNull();
    expect(tags(await row({ new_country: false }))).not.toContain(
      "New country",
    );
  });

  it("throttled reason label", async () => {
    const root = await row({ reason: "throttled" });
    expect(tags(root)).toContain("Too many attempts");
  });
});

describe("Users details show flags", () => {
  it("puts a flag next to the last login country", async () => {
    const fake = fakeApi({ users: { items: [user()] } });
    const el = await mount<EfUsers>("ef-users", fake);
    el.shadowRoot?.querySelector<HTMLButtonElement>("button.user")?.click();
    await settle(el);
    const line = el.shadowRoot?.querySelector("dialog p.last-login");
    const flag = line?.querySelector("ef-flag") as unknown as {
      code: string;
    } | null;
    expect(flag?.code).toBe("RU");
  });
});

describe("protection in Settings", () => {
  const rows = (root: ShadowRoot) =>
    Object.fromEntries(
      [...root.querySelectorAll("tr")].map((tr) => [
        tr.querySelector("th")?.textContent.trim(),
        tr.querySelector("td")?.textContent.replace(/\s+/g, " ").trim(),
      ]),
    );

  it("shows the ban, username and notification settings read-only", async () => {
    const s = status();
    s.settings.notify_targets = ["notify.mobile_app_anna", "notify.bo"];
    s.settings.ban_hours = 0;
    const el = await mount<EfSettings>("ef-settings", fakeApi({ status: s }));
    const root = el.shadowRoot as ShadowRoot;
    const headings = [...root.querySelectorAll("section h2")].map((h) =>
      h.textContent.trim(),
    );
    expect(headings).toEqual(
      expect.arrayContaining([
        "Bans",
        "Username protection",
        "Notifications",
        "Known countries",
      ]),
    );
    const r = rows(root);
    expect(r["Ban automatically"]).toBe("On");
    expect(r["Ban after"]).toBe("5 failed attempts within 15 min");
    expect(r["Ban length"]).toBe("Permanent");
    expect(r["Lengthen bans for repeat offenders"]).toBe("Off");
    expect(r["Protect usernames"]).toBe("On");
    expect(r["Slow down after"]).toBe(
      "10 failed attempts from at least 3 addresses within 15 min",
    );
    expect(r["IP address banned"]).toBe("On");
    expect(r["Ban ended"]).toBe("Off");
    expect(r["Login from a new country"]).toBe("On");
    expect(r["Username under attack"]).toBe("On");
    expect(r["Phone notifications"]).toBe("notify.mobile_app_anna, notify.bo");
    expect(root.querySelector("input, select, textarea")).toBeNull();
  });

  it("shows ban length in hours and no phone targets as None", async () => {
    const el = await mount<EfSettings>(
      "ef-settings",
      fakeApi({ status: status() }),
    );
    const r = rows(el.shadowRoot as ShadowRoot);
    expect(r["Ban length"]).toBe("24 h");
    expect(r["Phone notifications"]).toBe("None");
  });

  it("clears known countries only after confirming", async () => {
    const fake = fakeApi({ status: status(), clear_countries: { cleared: 4 } });
    const el = await mount<EfSettings>("ef-settings", fake);
    const root = el.shadowRoot as ShadowRoot;
    const button = [...root.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "Clear known countries",
    );
    button?.click();
    await settle(el);
    expect(fake.of("clear_countries")).toHaveLength(0);
    const confirmEl = root.querySelector("ef-confirm-dialog") as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    await confirmEl.updateComplete;
    const confirm = confirmEl.shadowRoot as ShadowRoot;
    expect(confirm.querySelector("dialog")?.open).toBe(true);
    confirm.querySelector<HTMLButtonElement>("button.confirm")?.click();
    await settle(el);
    await settle(el);
    expect(fake.of("clear_countries")).toEqual([
      { type: "electric_fence/clear_countries" },
    ]);
    expect(root.querySelector("ef-confirm-dialog")).toBeNull();
    expect(root.querySelector("[role=status]")?.textContent).toContain(
      "Known countries cleared",
    );
  });
});

describe("protection changes in the Audit", () => {
  const ipBan = (action: string, details: Record<string, unknown> = {}) =>
    change({
      kind: "ip_ban",
      action,
      target_id: "203.0.113.9",
      target_name: null,
      details,
      actor_ip: null,
    });
  const guard = (action: string, details: Record<string, unknown> = {}) =>
    change({
      kind: "login_protection",
      action,
      target_id: null,
      target_name: "admin",
      details,
      actor_ip: null,
    });

  it.each([
    [ipBan("banned"), "banned 203.0.113.9"],
    [ipBan("unbanned"), "unbanned 203.0.113.9"],
    [ipBan("expired"), "ban of 203.0.113.9 expired"],
    [ipBan("removed"), "ban of 203.0.113.9 was removed from ip_bans.yaml"],
    [guard("attack_started"), "username admin came under attack"],
    [guard("attack_ended"), "attack on admin ended"],
    [guard("slowdown_ended"), "ended the slow-down for admin"],
    [
      change({
        kind: "login_protection",
        action: "countries_cleared",
        target_id: null,
        target_name: null,
      }),
      "cleared known login countries",
    ],
  ])("protection sentence %#", (item, sentence) => {
    expect(changeSentence(item)).toBe(sentence);
  });

  async function row(item: ReturnType<typeof change>): Promise<ShadowRoot> {
    const el = document.createElement("ef-change-row");
    el.hass = fakeApi({}).hass;
    el.item = item;
    el.details = true;
    document.body.append(el);
    await el.updateComplete;
    return el.shadowRoot as ShadowRoot;
  }

  it("shows the ban's details", async () => {
    const root = await row(
      ipBan("banned", {
        failures: 5,
        expires_at: "2026-10-25T09:00:00+00:00",
        ban_number: 2,
      }),
    );
    const text = root.querySelector(".details")?.textContent ?? "";
    expect(text).toMatch(/Failed attempts:\s*5/);
    expect(text).toMatch(/Expires:\s*Oct 25, 2026/);
    expect(text).toMatch(/Ban number:\s*2/);
    const never = await row(
      ipBan("banned", { failures: 5, expires_at: null, ban_number: 4 }),
    );
    expect(never.querySelector(".details")?.textContent).toMatch(
      /Expires:\s*Never/,
    );
  });

  it("shows the attack's details", async () => {
    const root = await row(
      guard("slowdown_ended", {
        failures: 14,
        ip_count: 4,
        countries: ["CN", "RU"],
        reason: "ended_by_user",
      }),
    );
    const text = root.querySelector(".details")?.textContent ?? "";
    expect(text).toMatch(/Failed attempts:\s*14/);
    expect(text).toMatch(/Addresses:\s*4/);
    expect(text).toMatch(/Countries:\s*CN, RU/);
    expect(text).toMatch(/How it ended:\s*Ended by a user/);
  });

  it("shows no unknown IP chip for protection changes", async () => {
    expect((await row(ipBan("unbanned"))).textContent).not.toContain(
      "unknown IP",
    );
    expect((await row(guard("attack_started"))).textContent).not.toContain(
      "unknown IP",
    );
    expect((await row(change({ actor_ip: null }))).textContent).toContain(
      "unknown IP",
    );
    expect(EfChangeRow).toBeTruthy();
  });

  it("the Audit list shows the details lines too", async () => {
    const fake = fakeApi({
      changes: {
        items: [
          {
            ...ipBan("banned", {
              failures: 5,
              expires_at: null,
              ban_number: 1,
            }),
            id: 9,
          },
        ],
        next_before_id: null,
      },
      users: { items: [] },
    });
    const el = await mount<EfAudit>("ef-audit", fake);
    expect(el.shadowRoot?.textContent).toMatch(/Ban number:\s*1/);
  });
});

function type(root: ShadowRoot, name: string, value: string, event = "change") {
  const control = root.querySelector<HTMLInputElement>(`[name=${name}]`);
  (control as HTMLInputElement).value = value;
  control?.dispatchEvent(new Event(event, { bubbles: true }));
}

function dayBounds(from: string, to: string): { since: string; until: string } {
  const [fy, fm, fd] = from.split("-").map(Number) as [number, number, number];
  const [ty, tm, td] = to.split("-").map(Number) as [number, number, number];
  return {
    since: new Date(fy, fm - 1, fd).toISOString(),
    until: new Date(ty, tm - 1, td, 23, 59, 59, 999).toISOString(),
  };
}

describe("filter pane in Audit", () => {
  const api = () =>
    fakeApi({
      changes: { items: [change({ id: 3 })], next_before_id: null },
      users: { items: [user()] },
    });

  it("sends action, ip and search to electric_fence/changes", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const fake = api();
    const el = await mount<EfAudit>("ef-audit", fake);
    chooseOption(bar(el), "action", "installed");
    await settle(el);
    type(bar(el), "ip", "192.168.1.5");
    await settle(el);
    type(bar(el), "search", "tuya", "input");
    await vi.advanceTimersByTimeAsync(350);
    await settle(el);
    expect(fake.of("changes").at(-1)).toMatchObject({
      action: "installed",
      ip: "192.168.1.5",
      search: "tuya",
    });
  });

  it("offers the new kinds and actions", async () => {
    const el = await mount<EfAudit>("ef-audit", api());
    const pane = el.shadowRoot?.querySelector("ef-filter-pane") as unknown as {
      fields: { name: string; options?: { value: string; label: string }[] }[];
    };
    const kinds = pane.fields.find((f) => f.name === "kind")?.options ?? [];
    expect(kinds.map((o) => o.value)).toEqual(
      expect.arrayContaining([
        "app",
        "files",
        "integration_health",
        "forwarding",
      ]),
    );
    const actions = pane.fields.find((f) => f.name === "action")?.options ?? [];
    expect(actions[0]).toEqual({ value: "", label: "Any action" });
    expect(actions.map((o) => o.value)).toEqual(
      expect.arrayContaining([
        "files_changed",
        "forwarding_failed",
        "restarted",
      ]),
    );
    expect(actions.every((o) => o.label !== "" || o.value === "")).toBe(true);
    expect(actions.find((o) => o.value === "files_changed")?.label).toBe(
      "Files changed",
    );
  });

  it("sends the custom range at the day bounds, in local time", async () => {
    const fake = api();
    const el = await mount<EfAudit>("ef-audit", fake);
    chooseOption(bar(el), "range", "custom");
    await settle(el);
    type(bar(el), "from", "2026-10-01");
    await settle(el);
    type(bar(el), "to", "2026-10-03");
    await settle(el);
    const last = fake.of("changes").at(-1);
    expect(last).toMatchObject(dayBounds("2026-10-01", "2026-10-03"));
  });

  it("reads a reversed custom range in date order", async () => {
    const fake = api();
    const el = await mount<EfAudit>("ef-audit", fake);
    chooseOption(bar(el), "range", "custom");
    await settle(el);
    type(bar(el), "from", "2026-10-03");
    await settle(el);
    type(bar(el), "to", "2026-10-01");
    await settle(el);
    const last = fake.of("changes").at(-1);
    expect(last).toMatchObject(dayBounds("2026-10-01", "2026-10-03"));
  });

  it("hides from and to until the range is custom", async () => {
    const el = await mount<EfAudit>("ef-audit", api());
    expect(bar(el).querySelector("[name=from]")).toBeNull();
    chooseOption(bar(el), "range", "custom");
    await settle(el);
    expect(bar(el).querySelector("[name=from]")).not.toBeNull();
    expect(bar(el).querySelector("[name=to]")).not.toBeNull();
  });

  it("clears every filter with one refetch", async () => {
    const fake = api();
    const el = await mount<EfAudit>("ef-audit", fake);
    chooseOption(bar(el), "kind", "user");
    await settle(el);
    type(bar(el), "ip", "1.1.1.1");
    await settle(el);
    const before = fake.of("changes").length;
    bar(el).querySelector<HTMLButtonElement>("button.clear")?.click();
    await settle(el);
    expect(fake.of("changes")).toHaveLength(before + 1);
    const last = fake.of("changes").at(-1) as Record<string, unknown>;
    expect(last).not.toHaveProperty("kind");
    expect(last).not.toHaveProperty("ip");
    expect(last).toMatchObject({ include_system: false });
  });

  it("matches live changes on action, ip, custom range and search", () => {
    const base: AuditFilters = {
      kind: "",
      action: "",
      actor: "",
      ip: "",
      range: "all",
      from: "",
      to: "",
      system: false,
      search: "",
    };
    const now = Date.parse("2026-10-24T10:00:00Z");
    const item = change({
      action: "updated",
      target_name: "Tuya Cloud",
      details: { changed: ["Token"] },
    });
    const m = (over: Partial<AuditFilters>) =>
      matchesChange(item, { ...base, ...over }, now);
    expect(m({})).toBe(true);
    expect(m({ action: "updated" })).toBe(true);
    expect(m({ action: "added" })).toBe(false);
    expect(m({ ip: "192.168.1.5" })).toBe(true);
    expect(m({ ip: "10.0.0.1" })).toBe(false);
    expect(m({ search: "tuya CLOUD" })).toBe(true);
    expect(m({ search: "token" })).toBe(true);
    expect(m({ search: "anna" })).toBe(true);
    expect(m({ search: "nothing here" })).toBe(false);
    expect(m({ range: "custom", from: "2026-10-24", to: "2026-10-24" })).toBe(
      true,
    );
    expect(m({ range: "custom", from: "2026-10-25" })).toBe(false);
    expect(m({ range: "custom", to: "2026-10-23" })).toBe(false);
  });

  it("does not show a live change that misses the search", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const fake = api();
    const el = await mount<EfAudit>("ef-audit", fake);
    type(bar(el), "search", "tuya", "input");
    await vi.advanceTimersByTimeAsync(350);
    await settle(el);
    const rows = () => el.shadowRoot?.querySelectorAll("ef-change-row").length;
    const before = rows();
    fake.push({
      type: "change",
      item: change({
        id: null,
        target_name: "Hue",
        ts: new Date().toISOString(),
      }),
    });
    fake.push({
      type: "change",
      item: change({
        id: null,
        target_name: "Tuya 2",
        ts: new Date().toISOString(),
      }),
    });
    await settle(el);
    expect(rows()).toBe((before ?? 0) + 1);
  });
});

describe("filter pane in Activity", () => {
  const api = () =>
    fakeApi({
      logins: { items: [login({ id: 5 })], next_before_id: null },
      users: { items: [user()] },
    });

  it("sends search and ip to electric_fence/logins", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const fake = api();
    const el = await mount<EfActivity>("ef-activity", fake);
    type(bar(el), "ip", "203.0.113.7");
    await settle(el);
    type(bar(el), "search", "firefox", "input");
    await vi.advanceTimersByTimeAsync(350);
    await settle(el);
    expect(fake.of("logins").at(-1)).toMatchObject({
      ip: "203.0.113.7",
      search: "firefox",
    });
    expect(new URLSearchParams(location.search).get("search")).toBe("firefox");
  });

  it("sends the custom range at the day bounds and keeps it in the URL", async () => {
    history.replaceState(
      null,
      "",
      "/electric-fence/activity?range=custom&from=2026-10-01&to=2026-10-03",
    );
    const fake = api();
    await mount<EfActivity>("ef-activity", fake);
    expect(fake.of("logins")[0]).toMatchObject(
      dayBounds("2026-10-01", "2026-10-03"),
    );
    expect(
      filtersFromQuery("?range=custom&from=nope&to=2026-10-03"),
    ).toMatchObject({ range: "custom", from: "", to: "2026-10-03" });
  });

  it("matches live logins on the custom range and search", () => {
    const base = filtersFromQuery("");
    const now = Date.parse("2026-10-24T10:00:00Z");
    const item = login({ client_name: "Firefox", hostname: "Host.Example" });
    const m = (over: Partial<ActivityFilters>) =>
      matchesLogin(item, { ...base, range: "all", ...over }, now);
    expect(m({ search: "FIREfox" })).toBe(true);
    expect(m({ search: "host.example" })).toBe(true);
    expect(m({ search: "203.0.113" })).toBe(true);
    expect(m({ search: "chrome" })).toBe(false);
    expect(m({ range: "custom", from: "2026-10-25" })).toBe(false);
    expect(m({ range: "custom", to: "2026-10-23" })).toBe(false);
    expect(m({ range: "custom", from: "2026-10-23", to: "2026-10-25" })).toBe(
      true,
    );
  });
});
