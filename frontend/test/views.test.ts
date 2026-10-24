import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EfActivity, filtersFromQuery } from "../src/views/ef-activity.ts";
import { EfAudit } from "../src/views/ef-audit.ts";
import { EfSettings } from "../src/views/ef-settings.ts";
import { EfUsers } from "../src/views/ef-users.ts";
import { REFRESH_DELAY_MS } from "../src/views/live-view.ts";
import type { Status, UserItem } from "../src/types.ts";
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
    expect(el.shadowRoot?.querySelector(".keys")?.textContent).toBe("latitude");
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
