import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../src/components/ef-posture-list.ts";
import {
  INTEGRATIONS_REFRESH_MS,
  REFRESH_DELAY_MS,
  EfOverview,
} from "../src/views/ef-overview.ts";
import { changeSentence, postureText } from "../src/strings.ts";
import {
  attack,
  change,
  fakeApi,
  fakeHass,
  integration,
  login,
  overview,
  status,
} from "./helpers.ts";
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
  it("renders the five card titles", async () => {
    const el = await mount(fakeHass(overview()));
    expect(cards(el).map((c) => cardText(c).trim().split(/\s+/)[0])).toEqual([
      "Failed",
      "Active",
      "Settings",
      "Recent",
      "Posture",
    ]);
    expect(cards(el).map((c) => c.getAttribute("heading"))).toEqual([
      "Failed logins · 24h",
      "Active bans",
      "Settings changes · 7d",
      "Recent successful logins",
      "Posture",
    ]);
  });

  it("shows counts and rows from the fixture", async () => {
    const el = await mount(fakeHass(overview()));
    const [failed, , changes, success, posture] = cards(el);
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
    expect(text[2]).toBe("No settings changes in the last 7 days");
    expect(text[3]).toContain("No successful logins");
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
      "/electric-fence/bans",
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
    expect(cards(el)).toHaveLength(5);
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
    expect(cards(el)).toHaveLength(5);
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
      "RU",
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
    const rows = cards(el)[3]?.querySelectorAll("ef-login-row") ?? [];
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
        params: { threshold, electric_fence: false },
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
        params: { threshold: null, electric_fence: false },
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

describe("protection on the overview", () => {
  async function mountApi(
    answers: Parameters<typeof fakeApi>[0],
  ): Promise<{ el: EfOverview; fake: ReturnType<typeof fakeApi> }> {
    const fake = fakeApi({ status: status(), ...answers });
    const el = document.createElement("ef-overview");
    el.hass = fake.hass;
    document.body.append(el);
    for (let i = 0; i < 3; i += 1) {
      await settle(el);
      const confirm = el.shadowRoot?.querySelector("ef-confirm-dialog") as
        { updateComplete: Promise<unknown> } | null | undefined;
      await confirm?.updateComplete;
    }
    return { el, fake };
  }

  const attackRows = (el: EfOverview) => [
    ...(el.shadowRoot?.querySelectorAll(".attacks li") ?? []),
  ];

  it("overview shows active bans and end slow-down", async () => {
    let attacks = [attack()];
    const { el, fake } = await mountApi({
      overview: () =>
        overview({
          protection: {
            available: true,
            active_bans: 3,
            bans_24h: 4,
            attacks,
          },
        }),
      end_slowdown: () => {
        attacks = [];
        return { ended: true };
      },
    });
    const bans = cards(el)[1] as Element;
    expect(bans.getAttribute("heading")).toBe("Active bans");
    expect(bans.getAttribute("path")).toBe("/electric-fence/bans");
    expect(cardText(bans)).toContain("3");
    expect(bans.textContent).toContain("4 in the last 24 hours");

    const section = el.shadowRoot?.querySelector(".attacks");
    expect(section?.querySelector("h2")?.textContent).toBe("Under attack");
    const rows = attackRows(el);
    expect(rows).toHaveLength(1);
    const row = rows[0] as Element;
    expect(row.textContent).toContain("admin");
    expect(row.textContent).toContain("14 failed attempts");
    expect(row.textContent).toContain("4 addresses");

    row.querySelector<HTMLButtonElement>("button")?.click();
    for (let i = 0; i < 3; i += 1) {
      await settle(el);
    }
    expect(fake.of("end_slowdown")).toHaveLength(0);
    const confirm = el.shadowRoot?.querySelector("ef-confirm-dialog")
      ?.shadowRoot as ShadowRoot;
    expect(confirm.querySelector("dialog")?.open).toBe(true);
    expect(confirm.textContent?.replace(/\s+/g, " ")).toContain(
      "End the slow-down for admin?",
    );
    const before = fake.of("overview").length;
    confirm.querySelector<HTMLButtonElement>("button.confirm")?.click();
    for (let i = 0; i < 3; i += 1) {
      await settle(el);
    }
    expect(fake.of("end_slowdown")).toEqual([
      { type: "electric_fence/end_slowdown", username: "admin" },
    ]);
    expect(fake.of("overview").length).toBeGreaterThan(before);
    expect(el.shadowRoot?.querySelector(".attacks")).toBeNull();
    expect(el.shadowRoot?.querySelector("ef-confirm-dialog")).toBeNull();
  });

  it("hides Under attack when nothing is under attack", async () => {
    const { el } = await mountApi({ overview: overview() });
    expect(el.shadowRoot?.querySelector(".attacks")).toBeNull();
    expect(el.shadowRoot?.textContent).not.toContain("Under attack");
  });

  it("keeps the confirmation open with an error when ending fails", async () => {
    const { el } = await mountApi({
      overview: overview({
        protection: {
          available: true,
          active_bans: 0,
          bans_24h: 0,
          attacks: [attack()],
        },
      }),
    });
    attackRows(el)[0]?.querySelector<HTMLButtonElement>("button")?.click();
    for (let i = 0; i < 3; i += 1) {
      await settle(el);
    }
    const confirmEl = el.shadowRoot?.querySelector("ef-confirm-dialog");
    confirmEl?.shadowRoot
      ?.querySelector<HTMLButtonElement>("button.confirm")
      ?.click();
    for (let i = 0; i < 3; i += 1) {
      await settle(el);
      await (confirmEl as unknown as { updateComplete: Promise<unknown> })
        .updateComplete;
    }
    const confirm = confirmEl?.shadowRoot as ShadowRoot;
    expect(confirm.querySelector("dialog")?.open).toBe(true);
    expect(confirm.querySelector("[role=alert]")?.textContent).toContain(
      "Could not end the slow-down",
    );
  });

  it("says when IP banning is unavailable", async () => {
    const { el } = await mountApi({
      overview: overview({
        protection: {
          available: false,
          active_bans: 0,
          bans_24h: 0,
          attacks: [],
        },
      }),
    });
    expect(cards(el)[1]?.textContent).toContain("IP banning is unavailable");
  });
});

describe("protection posture sentences", () => {
  it("counts Electric Fence's auto-ban as on", () => {
    expect(
      postureText({
        id: "auto_ban_off",
        level: "ok",
        params: { threshold: null, electric_fence: true },
      }),
    ).toBe("Electric Fence bans an address after repeated failed logins.");
  });

  it("explains that Home Assistant's IP ban is off", () => {
    const text = postureText({
      id: "ip_ban_disabled",
      level: "warning",
      params: {},
    });
    expect(text).toContain("ip_ban_enabled");
    expect(text).not.toContain("not known");
  });
});

describe("custom integrations card", () => {
  const changed = integration({
    domain: "bad",
    name: "Bad one",
    source: "unverified",
    files: {
      status: "changed",
      reason: null,
      ref: "v1.0.0",
      version_before: null,
      report: {
        changed: ["a.py"],
        missing: ["b.py"],
        added: ["c.py"],
        unreadable: ["d.py"],
        symlinks: ["e.py"],
      },
    },
    health: {
      status: "unmaintained",
      level: "notice",
      last_updated: new Date(Date.now() - 167 * 86_400_000).toISOString(),
      reason: null,
    },
  });

  async function mountCard(
    integrations: unknown | (() => unknown),
    extra: Parameters<typeof fakeApi>[0] = {},
  ) {
    const fake = fakeApi({
      overview: overview(),
      status: status(),
      integrations,
      ...extra,
    });
    const el = document.createElement("ef-overview");
    el.hass = fake.hass;
    document.body.append(el);
    for (let i = 0; i < 4; i += 1) {
      await settle(el);
    }
    return { el, fake };
  }

  const card = (el: EfOverview) =>
    el.shadowRoot?.querySelector(".integrations") as HTMLElement;
  const squash = (t: string | null | undefined) =>
    t?.replace(/\s+/g, " ").trim() ?? "";

  it("shows a row with source, version, file status and health", async () => {
    const { el } = await mountCard({
      items: [changed, integration()],
      scanned_at: "2026-10-26T00:00:00+00:00",
      running: false,
      available: true,
    });
    const rows = card(el).querySelectorAll("li");
    expect(rows).toHaveLength(2);
    const first = squash(rows[0]?.textContent);
    expect(first).toContain("Bad one");
    expect(first).toContain("Unverified");
    expect(first).toContain("Files changed");
    expect(first).toContain("Not updated for 5 months");
    const second = squash(rows[1]?.textContent);
    expect(second).toContain("Marketplace");
    expect(second).toContain("1.2.0");
    expect(second).toContain("Files match");
    expect(second).toContain("Up to date");
  });

  it("opens a dialog listing the files", async () => {
    const { el } = await mountCard({
      items: [changed],
      scanned_at: null,
      running: false,
      available: true,
    });
    card(el).querySelector<HTMLButtonElement>("li button")?.click();
    await settle(el);
    const dialog = el.shadowRoot?.querySelector("ef-integration-dialog");
    expect(dialog).not.toBeNull();
    await (dialog as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const text = squash(dialog?.shadowRoot?.textContent);
    for (const part of [
      "Changed files a.py",
      "Missing files b.py",
      "Added files c.py",
      "Unreadable files d.py",
      "Symbolic links e.py",
      "owner/thing",
      "v1.0.0",
    ]) {
      expect(text).toContain(part);
    }
  });

  it("says why a check could not run", async () => {
    const { el } = await mountCard({
      items: [
        integration({
          files: {
            status: "unchecked",
            reason: "too_large",
            ref: null,
            version_before: null,
            report: null,
          },
        }),
      ],
      scanned_at: null,
      running: false,
      available: true,
    });
    expect(squash(card(el).textContent)).toContain("Couldn't check");
    card(el).querySelector<HTMLButtonElement>("li button")?.click();
    await settle(el);
    const dialog = el.shadowRoot?.querySelector("ef-integration-dialog");
    await (dialog as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    expect(squash(dialog?.shadowRoot?.textContent)).toContain(
      "The official copy is over the 25 MB limit",
    );
  });

  it("checks once and disables the button while running", async () => {
    vi.useFakeTimers();
    let running = false;
    const { el, fake } = await mountCard(
      () => ({ items: [changed], scanned_at: null, running, available: true }),
      {
        scan_integrations: () => {
          running = true;
          return { started: true };
        },
      },
    );
    const button = () =>
      card(el).querySelector<HTMLButtonElement>(
        "button.check",
      ) as HTMLButtonElement;
    expect(button().disabled).toBe(false);
    button().click();
    button().click();
    await settle(el);
    expect(fake.of("scan_integrations")).toHaveLength(1);
    expect(button().disabled).toBe(true);
    expect(squash(card(el).textContent)).toContain("Started");
    running = false;
    await vi.advanceTimersByTimeAsync(3000);
    await settle(el);
    expect(button().disabled).toBe(false);
  });

  it("reloads the card when a scan records a change", async () => {
    vi.useFakeTimers();
    let scanned: string | null = null;
    const { el, fake } = await mountCard(() => ({
      items: [changed],
      scanned_at: scanned,
      running: false,
      available: true,
    }));
    const before = fake.of("integrations").length;
    scanned = "2026-10-27T03:00:00+00:00";
    fake.push({ type: "change", item: change({ kind: "files" }) });
    await vi.advanceTimersByTimeAsync(REFRESH_DELAY_MS);
    await settle(el);
    expect(fake.of("integrations").length).toBe(before + 1);
    expect(squash(card(el).textContent)).toContain("Last check");
  });

  it("does not reload the card for other changes", async () => {
    vi.useFakeTimers();
    const { el, fake } = await mountCard({
      items: [changed],
      scanned_at: null,
      running: false,
      available: true,
    });
    const before = fake.of("integrations").length;
    fake.push({ type: "change", item: change({ kind: "settings" }) });
    await vi.advanceTimersByTimeAsync(REFRESH_DELAY_MS);
    await settle(el);
    expect(fake.of("integrations").length).toBe(before);
  });

  it("reloads the card now and then while the page is open", async () => {
    vi.useFakeTimers();
    const { el, fake } = await mountCard({
      items: [changed],
      scanned_at: null,
      running: false,
      available: true,
    });
    const before = fake.of("integrations").length;
    await vi.advanceTimersByTimeAsync(INTEGRATIONS_REFRESH_MS);
    await settle(el);
    expect(fake.of("integrations").length).toBe(before + 1);
    el.remove();
    await vi.advanceTimersByTimeAsync(INTEGRATIONS_REFRESH_MS * 2);
    expect(fake.of("integrations").length).toBe(before + 1);
  });

  it("says when a check ran recently", async () => {
    const { el } = await mountCard(
      { items: [changed], scanned_at: null, running: false, available: true },
      { scan_integrations: { started: false } },
    );
    card(el).querySelector<HTMLButtonElement>("button.check")?.click();
    await settle(el);
    expect(squash(card(el).textContent)).toContain(
      "A check is already running or ran in the last 5 minutes",
    );
  });

  it("shows an error and a retry when the command fails", async () => {
    let fail = true;
    const { el } = await mountCard(() => {
      if (fail) {
        throw new Error("boom");
      }
      return {
        items: [changed],
        scanned_at: null,
        running: false,
        available: true,
      };
    });
    expect(squash(card(el).textContent)).toContain(
      "Could not load this information from Home Assistant.",
    );
    expect(
      card(el).querySelector<HTMLButtonElement>("button.check")?.disabled,
    ).toBe(true);
    fail = false;
    const buttons = card(el).querySelectorAll<HTMLButtonElement>(
      "[role=alert] button",
    );
    buttons[0]?.click();
    await settle(el);
    expect(card(el).querySelector("[role=alert]")).toBeNull();
    expect(card(el).querySelectorAll("li")).toHaveLength(1);
  });

  it("says when integration checks are unavailable and disables Check now", async () => {
    const { el, fake } = await mountCard({
      items: [],
      scanned_at: null,
      running: false,
      available: false,
    });
    expect(squash(card(el).textContent)).toContain(
      "Integration checks unavailable",
    );
    const button = card(el).querySelector<HTMLButtonElement>("button.check");
    expect(button?.disabled).toBe(true);
    button?.click();
    await settle(el);
    expect(fake.of("scan_integrations")).toHaveLength(0);
  });

  it("says there are no custom integrations", async () => {
    const { el } = await mountCard({
      items: [],
      scanned_at: null,
      running: false,
      available: true,
    });
    expect(squash(card(el).textContent)).toContain("No custom integrations");
    expect(
      card(el).querySelector<HTMLButtonElement>("button.check")?.disabled,
    ).toBe(false);
  });

  it("keeps polling a long scan and re-enables the button when it ends", async () => {
    vi.useFakeTimers();
    let running = false;
    const { el } = await mountCard(
      () => ({ items: [changed], scanned_at: null, running, available: true }),
      {
        scan_integrations: () => {
          running = true;
          return { started: true };
        },
      },
    );
    const button = () =>
      card(el).querySelector<HTMLButtonElement>(
        "button.check",
      ) as HTMLButtonElement;
    button().click();
    await settle(el);
    await vi.advanceTimersByTimeAsync(2000 + 10_000 * 3);
    await settle(el);
    expect(button().disabled).toBe(true);
    running = false;
    await vi.advanceTimersByTimeAsync(10_000);
    await settle(el);
    expect(button().disabled).toBe(false);
  });

  it("follows a scan that is already running when the page opens", async () => {
    vi.useFakeTimers();
    let running = true;
    const { el } = await mountCard(() => ({
      items: [changed],
      scanned_at: null,
      running,
      available: true,
    }));
    const button = () =>
      card(el).querySelector<HTMLButtonElement>(
        "button.check",
      ) as HTMLButtonElement;
    expect(button().disabled).toBe(true);
    running = false;
    await vi.advanceTimersByTimeAsync(2000);
    await settle(el);
    expect(button().disabled).toBe(false);
  });

  it("gives up after five minutes and clears the stale state", async () => {
    vi.useFakeTimers();
    let running = false;
    const { el } = await mountCard(
      () => ({ items: [changed], scanned_at: null, running, available: true }),
      {
        scan_integrations: () => {
          running = true;
          return { started: true };
        },
      },
    );
    const button = () =>
      card(el).querySelector<HTMLButtonElement>(
        "button.check",
      ) as HTMLButtonElement;
    button().click();
    await settle(el);
    await vi.advanceTimersByTimeAsync(4 * 60_000);
    await settle(el);
    expect(button().disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(70_000);
    await settle(el);
    expect(button().disabled).toBe(false);
  });
});

describe("posture sentences for v0.3", () => {
  it("forwarding", () => {
    expect(
      postureText({
        id: "forwarding_failing",
        level: "warning",
        params: {
          last_error: "ConnectionRefusedError",
          failing_since: "2026-10-26T00:00:00+00:00",
          queued: 12,
          dropped: 3,
        },
      }),
    ).toBe(
      "Forwarding logs to your log server is failing (ConnectionRefusedError). 12 events are waiting and 3 were dropped.",
    );
    expect(
      postureText({
        id: "forwarding_failing",
        level: "ok",
        params: {
          last_error: null,
          failing_since: null,
          queued: 0,
          dropped: 0,
        },
      }),
    ).toBe("Forwarding logs to your log server is working.");
  });

  it("integration files, health and unchecked", () => {
    expect(
      postureText({
        id: "integration_files_changed",
        level: "warning",
        params: { integrations: ["a", "b"] },
      }),
    ).toBe("2 custom integrations differ from their official copy: a, b.");
    expect(
      postureText({
        id: "integration_files_changed",
        level: "ok",
        params: { integrations: [] },
      }),
    ).toBe("Custom integration files match their official copies.");
    expect(
      postureText({
        id: "integration_health",
        level: "critical",
        params: { removed: ["x"], archived: ["y"], unmaintained: [] },
      }),
    ).toBe(
      "1 custom integration was removed from the Marketplace: x. 1 is archived: y.",
    );
    expect(
      postureText({
        id: "integration_health",
        level: "ok",
        params: { removed: [], archived: [], unmaintained: [] },
      }),
    ).toBe("Custom integrations are maintained.");
    expect(
      postureText({
        id: "integration_checks_unchecked",
        level: "info",
        params: { integrations: ["z"] },
      }),
    ).toBe("1 custom integration could not be checked: z.");
  });

  it("styles info and critical levels", async () => {
    const el = document.createElement("ef-posture-list");
    el.items = [
      {
        id: "integration_checks_unchecked",
        level: "info",
        params: { integrations: ["z"] },
      },
      {
        id: "integration_health",
        level: "critical",
        params: { removed: ["x"], archived: [], unmaintained: [] },
      },
    ];
    document.body.append(el);
    await el.updateComplete;
    const icons = [...(el.shadowRoot?.querySelectorAll("ha-icon") ?? [])];
    expect(icons.map((i) => i.getAttribute("aria-label"))).toEqual([
      "Info",
      "Critical",
    ]);
    expect(icons.every((i) => (i.getAttribute("icon") ?? "") !== "")).toBe(
      true,
    );
    expect(icons.map((i) => i.getAttribute("icon"))).not.toContain("undefined");
  });
});
