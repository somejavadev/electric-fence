import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { EfFilterBar } from "../src/components/ef-filter-bar.ts";
import "../src/views/ef-activity.ts";
import "../src/views/ef-audit.ts";
import type { EfSettings } from "../src/views/ef-settings.ts";
import "../src/views/ef-settings.ts";
import "../src/views/ef-users.ts";
import { EfBans } from "../src/views/ef-bans.ts";
import { EfConfirmDialog } from "../src/components/ef-confirm-dialog.ts";
import { dialogStyles, viewStyles } from "../src/styles.ts";
import type { UserItem } from "../src/types.ts";
import { ban, change, fakeApi, login, status } from "./helpers.ts";
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

describe("Users rows", () => {
  it("use a chevron icon, not text arrows", async () => {
    const root = await mount("ef-users", fakeApi({ users: { items: [user] } }));
    expect(
      root.querySelector("button.user ha-icon")?.getAttribute("icon"),
    ).toBe("mdi:chevron-right");
    expect(root.textContent).not.toMatch(/[▸▾]/);
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
    [
      "ef-bans",
      {
        bans: {
          available: true,
          reason: null,
          items: [],
          next_before_id: null,
        },
      },
    ],
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

describe("tags use sentence case", () => {
  const REASONS = [
    "ok",
    "no_such_user",
    "wrong_password",
    "invalid_credentials",
    "wrong_2fa_code",
    "invalid_token",
    "blocked_inactive_user",
    "blocked_local_only",
  ] as const;

  function tagTexts(node: ShadowRoot): string[] {
    const out: string[] = [];
    const walk = (r: ShadowRoot) => {
      r.querySelectorAll(".tag").forEach((t) =>
        out.push((t.textContent ?? "").trim()),
      );
      r.querySelectorAll("*").forEach(
        (e) => e.shadowRoot && walk(e.shadowRoot),
      );
    };
    walk(node);
    return out;
  }

  const startsCapital = (t: string) => /^(\p{Lu}|×)/u.test(t);

  it.each(REASONS)("names reason %s in sentence case", async (reason) => {
    const { reasonLabel } = await import("../src/format.ts");
    expect(startsCapital(reasonLabel(reason))).toBe(true);
  });

  it("Activity rows", async () => {
    const root = await mount(
      "ef-activity",
      fakeApi({
        logins: {
          items: [
            login({ id: 1, result: "failed", reason: "wrong_password" }),
            login({
              id: 2,
              result: "success",
              reason: "ok",
              confirmed: false,
              mfa_module: null,
            }),
            login({
              id: 3,
              result: "success",
              reason: "ok",
              mfa_module: "totp",
            }),
          ],
          next_before_id: null,
        },
        users: { items: [user] },
      }),
    );
    const tags = tagTexts(root);
    expect(tags.length).toBeGreaterThan(2);
    for (const t of tags) {
      expect(startsCapital(t), t).toBe(true);
    }
  });

  it("Users rows", async () => {
    const root = await mount(
      "ef-users",
      fakeApi({
        users: {
          items: [
            { ...user, is_owner: true, local_only: true },
            { ...user, id: "u2", is_active: false, mfa_modules: ["totp"] },
          ],
        },
      }),
    );
    const tags = tagTexts(root);
    expect(tags.length).toBeGreaterThan(5);
    for (const t of tags) {
      expect(startsCapital(t), t).toBe(true);
    }
  });
});

const sheet = (styles: unknown): string =>
  ([styles] as unknown[])
    .flat(Infinity)
    .map((s) =>
      s && typeof s === "object" && "cssText" in s ? String(s.cssText) : "",
    )
    .join("\n");

describe("phone width", () => {
  const PHONE = 360;
  // 16px of panel padding on each side.
  const room = PHONE - 32;

  it.each([
    ["ef-bans", sheet(EfBans.styles)],
    ["ef-confirm-dialog", sheet(EfConfirmDialog.styles)],
  ])("phone width: %s sets no fixed width wider than a phone", (_n, css) => {
    const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
    for (const m of bare.matchAll(
      /(?:^|[;{\s])(min-width|width)\s*:\s*([^;}]+)/g,
    )) {
      const value = (m[2] ?? "").trim();
      if (/^min\(/.test(value)) {
        // min(560px, calc(100vw - 32px)) never exceeds the viewport.
        expect(value).toMatch(/100vw/);
        continue;
      }
      for (const px of value.matchAll(/(\d+)px/g)) {
        expect(Number(px[1]), `${m[1]}: ${value}`).toBeLessThanOrEqual(room);
      }
    }
    for (const m of bare.matchAll(/grid-template-columns\s*:\s*([^;}]+)/g)) {
      // Flexible tracks must be able to shrink below their content.
      expect((m[1] ?? "").replace(/minmax\(0,\s*1fr\)/g, "")).not.toMatch(
        /fr\b/,
      );
    }
  });

  it("phone width: bans view has no horizontal overflow at 360px", async () => {
    const css = sheet(EfBans.styles);
    // The list row: a shrinkable text column, and a stacked layout on phones.
    expect(rule(css, /^button\.ban$/)).toMatch(/minmax\(0,\s*1fr\)/);
    expect(css).toMatch(/@media\s*\(max-width:\s*600px\)/);
    // Long IPv6 addresses and host names wrap instead of widening the page.
    expect(rule(css, /^\.primary$/)).toMatch(
      /overflow-wrap\s*:\s*(anywhere|break-word)/,
    );
    expect(rule(css, /^\.secondary$/)).toMatch(
      /overflow-wrap\s*:\s*(anywhere|break-word)/,
    );
    expect(rule(css, /\.facts dd/)).toMatch(
      /overflow-wrap\s*:\s*(anywhere|break-word)/,
    );
    // Dialogs stay inside the viewport, like ha-dialog.
    expect(rule(dialogStyles.cssText, /^dialog$/)).toMatch(
      /width\s*:\s*min\([^;]*100vw/,
    );
    // The filters wrap onto more lines.
    expect(rule(filterCss, /^form$/)).toMatch(/flex-wrap\s*:\s*wrap/);
    const root = await mount(
      "ef-bans",
      fakeApi({
        bans: {
          available: true,
          reason: null,
          items: [ban({ ip: "2001:db8:ffff:ffff:ffff:ffff:ffff:ffff" })],
          next_before_id: null,
        },
      }),
    );
    expect(root.querySelector("table")).toBeNull();
    expect(root.querySelector(".card ul button.ban")).not.toBeNull();
  });

  it("Bans rows use sentence-case tags and a chevron", async () => {
    const root = await mount(
      "ef-bans",
      fakeApi({
        bans: {
          available: true,
          reason: null,
          items: [ban(), ban({ ip: "198.51.100.4", source: "other" })],
          next_before_id: null,
        },
      }),
    );
    const tags = [...root.querySelectorAll(".tag")].map((t) =>
      (t.textContent ?? "").trim(),
    );
    expect(tags).toEqual(["Electric Fence", "Other"]);
    expect(root.querySelector("button.ban ha-icon")?.getAttribute("icon")).toBe(
      "mdi:chevron-right",
    );
  });

  it("the confirmation looks like Home Assistant's dialog", () => {
    const css = sheet(EfConfirmDialog.styles);
    expect(rule(css, /^dialog$/)).toMatch(
      /border-radius\s*:\s*var\(--ha-border-radius-3xl/,
    );
    expect(rule(css, /\.actions$/)).toMatch(/justify-content\s*:\s*flex-end/);
  });
});
