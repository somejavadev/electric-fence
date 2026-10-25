import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REFRESH_DELAY_MS, EfOverview } from "../src/views/ef-overview.ts";
import { changeSentence, postureText } from "../src/strings.ts";
import { change, fakeHass, login, overview } from "./helpers.ts";
import type { FakeHass } from "./helpers.ts";

async function settle(el?: EfOverview): Promise<void> {
  for (let i = 0; i < 5; i += 1) {
    await Promise.resolve();
  }
  await el?.updateComplete;
}

async function mount(fake: FakeHass): Promise<EfOverview> {
  const el = document.createElement("ef-overview");
  el.hass = fake.hass;
  document.body.append(el);
  await settle(el);
  return el;
}

const cards = (el: EfOverview) => [
  ...(el.shadowRoot?.querySelectorAll("ef-answer-card") ?? []),
];
const cardText = (card: Element) => card.shadowRoot?.textContent ?? "";

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("ef-overview", () => {
  it("renders the four card titles", async () => {
    const el = await mount(fakeHass(overview()));
    expect(cards(el).map((c) => cardText(c).trim().split(/\s+/)[0])).toEqual([
      "Failed",
      "Settings",
      "Recent",
      "Posture",
    ]);
    expect(cards(el).map((c) => c.getAttribute("heading"))).toEqual([
      "Failed logins · 24h",
      "Settings changes · 7d",
      "Recent successful logins",
      "Posture",
    ]);
  });

  it("shows counts and rows from the fixture", async () => {
    const el = await mount(fakeHass(overview()));
    const [failed, changes, success, posture] = cards(el);
    expect(cardText(failed as Element)).toContain("17");
    expect(cardText(changes as Element)).toContain("3");
    expect(failed?.querySelectorAll("ef-login-row")).toHaveLength(1);
    expect(changes?.querySelectorAll("ef-change-row")).toHaveLength(1);
    expect(success?.querySelectorAll("ef-login-row")).toHaveLength(1);
    expect(posture?.querySelector("ef-posture-list")).not.toBeNull();
    expect(cardText(posture as Element)).toContain("1 to check");
  });

  it("shows empty states for empty lists", async () => {
    const el = await mount(
      fakeHass(
        overview({
          failed_24h: { count: 0, items: [] },
          changes_7d: { count: 0, items: [] },
          recent_success: { items: [] },
          posture: [],
        }),
      ),
    );
    const text = cards(el).map((c) => c.textContent.trim());
    expect(text[0]).toBe("No failed logins in the last 24 hours");
    expect(text[1]).toBe("No settings changes in the last 7 days");
    expect(text[2]).toContain("No successful logins");
  });

  it("shows loading before data arrives", async () => {
    const fake = fakeHass(overview());
    const el = document.createElement("ef-overview");
    el.hass = fake.hass;
    document.body.append(el);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).toContain("Loading…");
  });

  it("shows an error with a retry that works", async () => {
    const fake = fakeHass(overview());
    const send = fake.hass.connection.sendMessagePromise.bind(
      fake.hass.connection,
    );
    let failing = true;
    fake.hass.connection.sendMessagePromise = ((m: { type: string }) =>
      failing && m.type === "electric_fence/overview"
        ? Promise.reject({ code: "boom" })
        : send(m)) as typeof send;
    const el = await mount(fake);
    expect(el.shadowRoot?.querySelector("[role=alert]")).not.toBeNull();
    failing = false;
    el.shadowRoot
      ?.querySelector<HTMLButtonElement>("[role=alert] button")
      ?.click();
    await settle(el);
    expect(el.shadowRoot?.querySelector("[role=alert]")).toBeNull();
    expect(cardText(cards(el)[0] as Element)).toContain("17");
  });

  it("navigates when a card is selected", async () => {
    const el = await mount(fakeHass(overview()));
    const seen: string[] = [];
    const listener = () => seen.push(location.pathname + location.search);
    window.addEventListener("location-changed", listener);
    for (const card of cards(el)) {
      card.shadowRoot?.querySelector<HTMLElement>("button")?.click();
    }
    window.removeEventListener("location-changed", listener);
    expect(seen).toEqual([
      "/electric-fence/activity?result=failed_or_blocked&range=24h",
      "/electric-fence/audit",
      "/electric-fence/activity?result=success&range=all",
      "/electric-fence/settings",
    ]);
  });

  it("refreshes the count after a push, debounced to one second", async () => {
    vi.useFakeTimers();
    const fake = fakeHass(overview());
    const el = await mount(fake);
    const fetches = () =>
      fake.calls.filter((c) => c === "electric_fence/overview").length;
    expect(fetches()).toBe(1);
    fake.setOverview(
      overview({ failed_24h: { count: 18, items: [login({ id: 2 })] } }),
    );
    fake.push({ type: "login", item: login({ id: null }) });
    fake.push({ type: "login", item: login({ id: null }) });
    await vi.advanceTimersByTimeAsync(REFRESH_DELAY_MS - 1);
    expect(fetches()).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    await settle(el);
    expect(fetches()).toBe(2);
    expect(cardText(cards(el)[0] as Element)).toContain("18");
  });

  it("shows the unloaded state and subscribes again on retry", async () => {
    const fake = fakeHass(overview());
    const el = await mount(fake);
    expect(fake.subscriptions()).toBe(1);
    fake.push({ type: "unloaded" });
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).toContain(
      "Electric Fence was unloaded or reloaded",
    );
    expect(cards(el)).toHaveLength(0);
    el.shadowRoot?.querySelector("button")?.click();
    await settle(el);
    expect(fake.subscriptions()).toBe(2);
    expect(cards(el)).toHaveLength(4);
  });

  it("subscribes again when the page becomes visible after an unload", async () => {
    const fake = fakeHass(overview());
    const el = await mount(fake);
    fake.push({ type: "unloaded" });
    await el.updateComplete;
    document.dispatchEvent(new Event("visibilitychange"));
    await settle(el);
    expect(fake.subscriptions()).toBe(2);
  });

  it("keeps the unloaded state through hass updates", async () => {
    const fake = fakeHass(overview());
    const el = await mount(fake);
    fake.push({ type: "unloaded" });
    await el.updateComplete;
    const fetches = fake.calls.length;
    const subs = fake.subscriptions();
    for (let i = 0; i < 3; i += 1) {
      el.hass = { ...fake.hass };
      await settle(el);
    }
    expect(fake.calls).toHaveLength(fetches);
    expect(fake.subscriptions()).toBe(subs);
    expect(el.shadowRoot?.textContent).toContain("unloaded or reloaded");
    expect(cards(el)).toHaveLength(0);
  });

  it("says live updates are unavailable when subscribing fails", async () => {
    const fake = fakeHass(overview());
    fake.hass.connection.subscribeMessage = (() =>
      Promise.reject({ code: "boom" })) as never;
    const el = await mount(fake);
    expect(el.shadowRoot?.textContent).toContain(
      "Live updates are unavailable",
    );
    expect(cards(el)).toHaveLength(4);
  });

  it("stops everything when removed", async () => {
    vi.useFakeTimers();
    const fake = fakeHass(overview());
    const el = await mount(fake);
    const fetches = () =>
      fake.calls.filter((c) => c === "electric_fence/overview").length;
    fake.push({ type: "login", item: login({ id: null }) });
    el.remove();
    fake.push({ type: "change", item: change({ id: null }) });
    await vi.advanceTimersByTimeAsync(REFRESH_DELAY_MS * 3);
    expect(fetches()).toBe(1);
  });

  it("drops a response that arrives after removal", async () => {
    const fake = fakeHass(overview());
    let release: (value: unknown) => void = () => undefined;
    fake.hass.connection.sendMessagePromise = ((m: { type: string }) =>
      m.type === "electric_fence/overview"
        ? new Promise((resolve) => {
            release = resolve;
          })
        : Promise.resolve(null)) as never;
    const el = await mount(fake);
    el.remove();
    release(overview());
    await settle();
    expect(
      (el as unknown as { _overview?: unknown })._overview,
    ).toBeUndefined();
  });

  it("unsubscribes when removed", async () => {
    const fake = fakeHass(overview());
    const el = await mount(fake);
    el.remove();
    expect(fake.unsubscribed()).toBe(1);
  });

  it("shows the geo attribution when set", async () => {
    const el = await mount(fakeHass(overview(), "IP geolocation by DB-IP"));
    expect(el.shadowRoot?.querySelector(".attribution")?.textContent).toBe(
      "IP geolocation by DB-IP",
    );
  });

  it("shows no attribution line when none is set", async () => {
    const el = await mount(fakeHass(overview()));
    expect(el.shadowRoot?.querySelector(".attribution")).toBeNull();
  });
});

describe("rows", () => {
  it("shows local only for local sources, else the flag or unknown country", async () => {
    const el = await mount(
      fakeHass(
        overview({
          failed_24h: {
            count: 4,
            items: [
              login({ country_code: "RU" }),
              login({ country_code: null, source: "local" }),
              login({ country_code: null, source: "remote" }),
              login({ country_code: null, source: null }),
            ],
          },
        }),
      ),
    );
    const rows = el.shadowRoot?.querySelectorAll("ef-login-row") ?? [];
    const where = [...rows].map(
      (r) => r.shadowRoot?.querySelector(".where")?.textContent,
    );
    expect(where.slice(0, 4)).toEqual([
      "🇷🇺 RU",
      "local",
      "unknown country",
      "unknown country",
    ]);
  });

  it("tags successful logins with MFA or no MFA", async () => {
    const el = await mount(
      fakeHass(
        overview({
          recent_success: {
            items: [
              login({ result: "success", mfa_module: "totp" }),
              login({ result: "success", mfa_module: null }),
            ],
          },
        }),
      ),
    );
    const rows = cards(el)[2]?.querySelectorAll("ef-login-row") ?? [];
    expect(
      [...rows].map((r) => r.shadowRoot?.querySelector(".tag")?.textContent),
    ).toEqual(["Two-factor", "No two-factor"]);
  });
});

describe("sentences", () => {
  const base = change();
  it.each([
    [{ action: "added" }, "added integration Tuya"],
    [{ action: "removed" }, "removed integration Tuya"],
    [{ action: "reconfigured" }, "reconfigured Tuya"],
    [{ action: "options_changed" }, "changed options of Tuya"],
    [{ action: "enabled" }, "enabled Tuya"],
    [{ action: "disabled" }, "disabled Tuya"],
    [{ action: "password_changed" }, "changed the password of Tuya"],
    [{ action: "updated" }, "changed Tuya"],
    [{ kind: "user", action: "added" }, "added user Tuya"],
    [{ kind: "user", action: "disabled" }, "disabled user Tuya"],
    [{ kind: "backup", action: "updated" }, "changed backup settings"],
    [{ kind: "backup", action: "removed" }, "deleted a backup"],
    [
      { kind: "backup", action: "restore_requested" },
      "requested a backup restore",
    ],
    [{ kind: "http", action: "updated" }, "changed HTTP settings"],
    [{ kind: "http", action: "withdrawn" }, "withdrew pending HTTP settings"],
    [{ kind: "http", action: "confirmed" }, "confirmed HTTP settings"],
    [{ kind: "network", action: "updated" }, "changed network settings"],
    [{ kind: "analytics", action: "updated" }, "changed analytics settings"],
    [{ kind: "logger", action: "updated" }, "changed log levels"],
    [{ kind: "recorder", action: "disabled" }, "disabled the recorder"],
    [{ kind: "recorder", action: "enabled" }, "enabled the recorder"],
    [{ kind: "recorder", action: "purged" }, "purged the recorder"],
    [{ kind: "future", action: "updated" }, "changed future"],
    [{ kind: "http", action: "teleported" }, "changed HTTP settings"],
    [
      { kind: "core_config", details: { keys: ["latitude", "unit_system"] } },
      "changed core settings (latitude, unit_system)",
    ],
  ] as const)("%j", (over, expected) => {
    expect(changeSentence({ ...base, ...over } as typeof base)).toBe(expected);
  });

  it("explains a posture check that failed", () => {
    expect(
      postureText({
        id: "auto_ban_off",
        level: "error",
        params: { check_failed: "KeyError" },
      }),
    ).toContain("could not run (KeyError)");
  });

  it("says auto-ban is off, without a number, for a threshold of zero or less", () => {
    for (const threshold of [0, -1]) {
      const text = postureText({
        id: "auto_ban_off",
        level: "warning",
        params: { threshold },
      });
      expect(text).toContain("turned off");
      expect(text).not.toMatch(/-?\d/);
    }
  });

  it("uses Home Assistant, never the abbreviation", () => {
    const texts = [
      postureText({
        id: "auto_ban_off",
        level: "warning",
        params: { threshold: null },
      }),
      postureText({
        id: "proxy_suspected",
        level: "warning",
        params: { ip: "10.0.0.1", users: 4, use_x_forwarded_for: false },
      }),
    ];
    for (const text of texts) {
      expect(text).toContain("Home Assistant");
      expect(text).not.toMatch(/\bHA\b/);
    }
  });
});

beforeEach(() => {
  history.replaceState(null, "", "/");
});
