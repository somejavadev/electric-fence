import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { EfFilterBar } from "../src/components/ef-filter-bar.ts";
import "../src/views/ef-activity.ts";
import "../src/views/ef-audit.ts";
import type { EfSettings } from "../src/views/ef-settings.ts";
import "../src/views/ef-settings.ts";
import "../src/views/ef-users.ts";
import { viewStyles } from "../src/styles.ts";
import type { UserItem } from "../src/types.ts";
import { change, fakeApi, login, status } from "./helpers.ts";
import type { FakeApi } from "./helpers.ts";

// Home Assistant's look for the Activity, Users, Audit and Settings tabs:
// its field, button, card and icon styling, and readable sentence-case text.

type Mounted = HTMLElement & { updateComplete: Promise<unknown> };

async function mount(tag: string, api: FakeApi): Promise<ShadowRoot> {
  const el = document.createElement(tag) as Mounted;
  (el as unknown as { hass: unknown }).hass = api.hass;
  document.body.append(el);
  for (let i = 0; i < 3; i += 1) {
    for (let j = 0; j < 8; j += 1) {
      await Promise.resolve();
    }
    await el.updateComplete;
  }
  return el.shadowRoot as ShadowRoot;
}

function rule(css: string, selector: RegExp): string {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return [...bare.matchAll(/([^{}]+)\{([^}]*)\}/g)]
    .filter((m) => selector.test((m[1] ?? "").trim()))
    .map((m) => m[2] ?? "")
    .join(";");
}

const filterCss = EfFilterBar.styles
  ? [EfFilterBar.styles]
      .flat()
      .map((s) => String((s as { cssText: string }).cssText))
      .join("\n")
  : "";
const viewCss = viewStyles.cssText;

const user: UserItem = {
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
  sessions: [],
};

beforeEach(() => {
  history.replaceState(null, "", "/electric-fence/activity");
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("filter fields look like Home Assistant's", () => {
  it("are compact outlined fields", () => {
    const field = rule(filterCss, /(^|,)\s*select\b/);
    // Home Assistant's outline, at a dense height so the bar fits one row.
    expect(field).toMatch(/(^|;)\s*height\s*:\s*40px/);
    expect(field).toMatch(/font-size\s*:\s*var\(--ha-font-size-m/);
    expect(field).toMatch(/border-radius\s*:\s*var\(--ha-border-radius-sm/);
    expect(field).toMatch(/border\s*:[^;]*var\(--outline-color/);
  });

  it("show focus with a 2px primary outline", () => {
    expect(rule(filterCss, /:focus/)).toMatch(/2px[^;]*var\(--primary-color/);
  });

  it("colour the checkbox with the primary colour", () => {
    expect(rule(filterCss, /checkbox/)).toMatch(
      /accent-color\s*:\s*var\(--primary-color/,
    );
  });
});

describe("buttons look like Home Assistant's", () => {
  it("are pill shaped, 40px tall, medium weight", () => {
    const button = rule(viewCss, /^button$/);
    expect(button).toMatch(/border-radius\s*:\s*var\(--ha-border-radius-pill/);
    expect(button).toMatch(/min-height\s*:\s*40px/);
    expect(button).toMatch(/font-weight\s*:\s*var\(--ha-font-weight-medium/);
  });
});

describe("lists sit in cards", () => {
  it("Activity puts its rows in a card", async () => {
    const root = await mount(
      "ef-activity",
      fakeApi({
        logins: { items: [login({ id: 5 })], next_before_id: null },
        users: { items: [user] },
      }),
    );
    expect(root.querySelector(".card ul ef-login-row")).not.toBeNull();
  });

  it("Activity puts its empty state in a card", async () => {
    const root = await mount(
      "ef-activity",
      fakeApi({
        logins: { items: [], next_before_id: null },
        users: { items: [user] },
      }),
    );
    expect(root.querySelector(".card .empty")).not.toBeNull();
  });

  it("Audit puts its rows in a card", async () => {
    const root = await mount(
      "ef-audit",
      fakeApi({
        changes: { items: [change({ id: 3 })], next_before_id: null },
        users: { items: [user] },
      }),
    );
    expect(root.querySelector(".card ul ef-change-row")).not.toBeNull();
  });

  it("Audit puts its empty state in a card", async () => {
    const root = await mount(
      "ef-audit",
      fakeApi({
        changes: { items: [], next_before_id: null },
        users: { items: [user] },
      }),
    );
    expect(root.querySelector(".card .empty")).not.toBeNull();
  });
});

describe("Users expand toggles", () => {
  it("use chevron icons, not text arrows", async () => {
    const root = await mount("ef-users", fakeApi({ users: { items: [user] } }));
    const toggle = root.querySelector("button[aria-expanded]");
    expect(toggle?.querySelector("ha-icon")?.getAttribute("icon")).toBe(
      "mdi:chevron-right",
    );
    expect(root.textContent).not.toMatch(/[▸▾]/);
    (toggle as HTMLButtonElement).click();
    await Promise.resolve();
    await (root.host as Mounted).updateComplete;
    expect(toggle?.querySelector("ha-icon")?.getAttribute("icon")).toBe(
      "mdi:chevron-down",
    );
  });
});

describe("Settings uses readable sentence-case text", () => {
  it("names collectors and capitalises values", async () => {
    const s = status();
    s.collectors = [
      { name: "login", enabled: true, reason: [] },
      { name: "auth_failures", enabled: true, reason: [] },
      { name: "attribution", enabled: true, reason: [] },
      { name: "config_entries", enabled: false, reason: [] },
      { name: "users", enabled: true, reason: [] },
      { name: "core_config", enabled: true, reason: [] },
      { name: "from_the_future", enabled: true, reason: [] },
    ];
    s.geo = {
      provider: "dbip_lite",
      db_date: null,
      last_error: null,
      attribution: null,
    };
    const root = await mount("ef-settings", fakeApi({ status: s }));
    const names = [...root.querySelectorAll("tbody th[scope=row]")].map((th) =>
      th.textContent?.trim(),
    );
    expect(names).toEqual(
      expect.arrayContaining([
        "Logins",
        "Failed authentication",
        "Who made changes",
        "Integrations",
        "Users",
        "Core settings",
        "from_the_future",
      ]),
    );
    const text = root.textContent ?? "";
    expect(text).toMatch(/\bOn\b/);
    expect(text).toMatch(/\bOff\b/);
    expect(text).toMatch(/Last error\s*None/);
    expect(text).toMatch(/Database date\s*Unknown/);
    expect(root.host as EfSettings).toBeTruthy();
  });
});

describe("tab names are not repeated as page headings", () => {
  it.each([
    ["ef-activity", { logins: { items: [], next_before_id: null } }],
    ["ef-audit", { changes: { items: [], next_before_id: null } }],
    ["ef-users", {}],
    ["ef-settings", { status: status() }],
  ] as const)(
    "%s keeps its heading for screen readers only",
    async (tag, extra) => {
      const root = await mount(
        tag,
        fakeApi({ users: { items: [user] }, ...extra }),
      );
      const heading = root.querySelector("h2");
      expect(heading?.classList.contains("visually-hidden")).toBe(true);
      expect(rule(viewCss, /\.visually-hidden/)).toMatch(/clip/);
    },
  );
});
