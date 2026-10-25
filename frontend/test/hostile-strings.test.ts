import { afterEach, describe, expect, it } from "vitest";
import "../src/components/ef-login-row.ts";
import { EfLoginRow } from "../src/components/ef-login-row.ts";
import type { HomeAssistant } from "../src/types.ts";
import "../src/views/ef-activity.ts";
import "../src/views/ef-audit.ts";
import "../src/views/ef-overview.ts";
import "../src/views/ef-settings.ts";
import "../src/views/ef-users.ts";
import {
  change,
  fakeApi,
  fakeHass,
  login,
  overview,
  status,
} from "./helpers.ts";

const hass = {
  locale: { language: "en" },
  config: { time_zone: "UTC" },
  language: "en",
  connection: {},
} as unknown as HomeAssistant;

const HOSTILE = [
  "<img src=x onerror=alert(1)>",
  "javascript:alert(1)",
  "‮evil",
  "a".repeat(10_000),
];

async function row(
  typed: string,
  variant: "failed" | "success" = "failed",
): Promise<EfLoginRow> {
  const el = document.createElement("ef-login-row");
  el.hass = hass;
  el.variant = variant;
  el.item = login({ username_typed: typed, client_name: typed });
  document.body.append(el);
  await el.updateComplete;
  return el;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("hostile typed usernames", () => {
  it.each(HOSTILE)("%#: stays text and is truncated", async (typed) => {
    for (const variant of ["failed", "success"] as const) {
      const el = await row(typed, variant);
      const root = el.shadowRoot as ShadowRoot;
      expect(root.querySelector("img")).toBeNull();
      expect(root.querySelector("[onerror]")).toBeNull();
      expect(root.querySelector("a[href]")).toBeNull();
      expect(root.querySelector('[href^="javascript:" i]')).toBeNull();
      const name = root.querySelector<HTMLElement>(".username");
      const visible = name?.textContent ?? "";
      expect(visible.length).toBeLessThanOrEqual(65);
      expect(name?.getAttribute("title")).toBe(typed);
      if (typed.length <= 64) {
        expect(root.textContent).toContain(typed);
      } else {
        expect(visible.startsWith("a".repeat(64))).toBe(true);
      }
      document.body.replaceChildren();
    }
  });
});

// Every field a stranger or an admin can type, each with markup, a script URL
// and a right-to-left override.
const hostile = (label: string) =>
  `\u202e<img src=x onerror=alert(1)>javascript:alert(1)-${label}`;

const allShadowElements = (root: ParentNode): Element[] =>
  [...root.querySelectorAll("*")].flatMap((el) => [
    el,
    ...(el.shadowRoot ? allShadowElements(el.shadowRoot) : []),
  ]);

describe("hostile values across the whole overview", () => {
  it("renders every field as text, isolated in bdi", async () => {
    const labels = [
      "user",
      "client",
      "ip",
      "host",
      "actor",
      "target",
      "key",
      "admin",
      "geoerror",
      "collector",
      "reason",
      "storage",
      "proxy",
      "attribution",
      "actorip",
      "successip",
      "successuser",
      "successname",
      "tokenurl",
    ] as const;
    const h = Object.fromEntries(labels.map((l) => [l, hostile(l)])) as Record<
      (typeof labels)[number],
      string
    >;
    const fake = fakeHass(
      overview({
        failed_24h: {
          count: 1,
          items: [
            login({
              username_typed: h.user,
              ip: h.ip,
              hostname: h.host,
              client_name: h.client,
              user_agent: h.client,
            }),
            login({
              reason: "invalid_token",
              username_typed: null,
              url: h.tokenurl,
            }),
          ],
        },
        changes_7d: {
          count: 3,
          items: [
            change({
              actor_name: h.actor,
              actor_ip: h.actorip,
              target_name: h.target,
            }),
            change({
              kind: "user",
              actor_name: h.actor,
              target_name: h.target,
            }),
            change({
              kind: "core_config",
              action: "updated",
              details: { keys: [h.key] },
            }),
          ],
        },
        recent_success: {
          items: [
            login({
              result: "success",
              username_typed: h.successuser,
              user_name: h.successname,
              client_name: h.client,
              ip: h.successip,
              mfa_module: null,
            }),
          ],
        },
        posture: [
          {
            id: "admins_without_mfa",
            level: "warning",
            params: { users: [{ id: "1", name: h.admin }] },
          },
          {
            id: "geoip_missing",
            level: "warning",
            params: { provider: "dbip_lite", last_error: h.geoerror },
          },
          {
            id: "collector_disabled",
            level: "error",
            params: { collector: h.collector, reasons: [h.reason] },
          },
          {
            id: "storage_error",
            level: "error",
            params: {
              storage_errors: 2,
              last_error: h.storage,
              last_error_at: null,
            },
          },
          {
            id: "proxy_suspected",
            level: "warning",
            params: { ip: h.proxy, users: 3, use_x_forwarded_for: false },
          },
        ],
      }),
      h.attribution,
    );
    const el = document.createElement("ef-overview");
    el.hass = fake.hass;
    document.body.append(el);
    for (let i = 0; i < 6; i += 1) {
      await Promise.resolve();
    }
    await el.updateComplete;
    await new Promise((r) => setTimeout(r, 0));

    const elements = allShadowElements(el.shadowRoot as ShadowRoot);
    const tags = new Set(elements.map((e) => e.localName));
    expect(tags).toEqual(
      new Set([
        "ef-answer-card",
        "ef-login-row",
        "ef-change-row",
        "ef-posture-list",
        // Posture icons: the icon name is a constant, never data.
        "ha-icon",
        "article",
        "header",
        "button",
        "span",
        "slot",
        "div",
        "bdi",
        "ul",
        "li",
        "p",
        ...(tags.has("style") ? ["style"] : []),
      ]),
    );
    expect(elements.some((e) => e.hasAttribute("onerror"))).toBe(false);
    const icons = elements
      .filter((e) => e.localName === "ha-icon")
      .map((e) => e.getAttribute("icon"));
    expect(icons.length).toBeGreaterThan(0);
    for (const icon of icons) {
      expect([
        "mdi:check-circle-outline",
        "mdi:alert-circle-outline",
        "mdi:close-circle-outline",
      ]).toContain(icon);
    }

    const shown = allShadowElements(el.shadowRoot as ShadowRoot)
      .map((e) => e.textContent)
      .join("\n");
    // Rendered fields (hostname, user agent and the success IP are never
    // shown, and the success row names the user instead of the typed text).
    for (const label of labels.filter(
      (l) => l !== "host" && l !== "successip" && l !== "successuser",
    )) {
      expect(shown, label).toContain(h[label]);
    }
    // Every text node carrying the override sits inside a bdi.
    const carriers = elements.filter((e) =>
      [...e.childNodes].some(
        (n) => n.nodeType === 3 && (n.textContent ?? "").includes("\u202e"),
      ),
    );
    expect(carriers.length).toBeGreaterThan(10);
    for (const carrier of carriers) {
      expect(
        carrier.closest("bdi"),
        `<${carrier.localName} class="${carrier.className}">`,
      ).not.toBeNull();
    }
  });
});

describe("hostile values in the other tabs", () => {
  const H = (label: string) => hostile(label);

  async function render(tag: string, fake: ReturnType<typeof fakeApi>) {
    const el = document.createElement(tag) as HTMLElement & {
      hass: unknown;
      updateComplete: Promise<unknown>;
    };
    el.hass = fake.hass;
    document.body.append(el);
    for (let i = 0; i < 8; i += 1) {
      await Promise.resolve();
    }
    await el.updateComplete;
    await new Promise((r) => setTimeout(r, 0));
    return el;
  }

  function inspect(el: HTMLElement, tags: string[], labels: string[]) {
    const elements = allShadowElements(el.shadowRoot as ShadowRoot);
    const names = new Set(elements.map((e) => e.localName));
    expect(names).toEqual(
      new Set([...tags, ...(names.has("style") ? ["style"] : [])]),
    );
    expect(elements.some((e) => e.hasAttribute("onerror"))).toBe(false);
    expect(elements.some((e) => e.localName === "img")).toBe(false);
    const shown = elements.map((e) => e.textContent).join("\n");
    for (const label of labels) {
      expect(shown, label).toContain(H(label));
    }
    const carriers = elements.filter((e) =>
      [...e.childNodes].some(
        (n) => n.nodeType === 3 && (n.textContent ?? "").includes("‮"),
      ),
    );
    expect(carriers.length).toBeGreaterThanOrEqual(labels.length);
    for (const carrier of carriers) {
      expect(
        carrier.closest("bdi"),
        `<${carrier.localName} class="${carrier.className}">`,
      ).not.toBeNull();
    }
  }

  const filterBarTags = [
    "ef-filter-bar",
    "form",
    "label",
    "select",
    "option",
    "input",
  ];

  it("activity, with every detail line expanded", async () => {
    const fake = fakeApi({
      logins: {
        items: [
          login({
            username_typed: H("username"),
            ip: H("ip"),
            url: H("url"),
            hostname: H("host"),
            user_agent: H("agent"),
            provider: H("provider"),
            mfa_module: H("mfa"),
          }),
          login({
            result: "success",
            username_typed: H("typed"),
            user_name: H("name"),
            client_name: H("client"),
          }),
        ],
        next_before_id: null,
      },
      users: { items: [] },
    });
    const el = await render("ef-activity", fake);
    for (const row of el.shadowRoot?.querySelectorAll("ef-login-row") ?? []) {
      row.shadowRoot
        ?.querySelector<HTMLButtonElement>("button.toggle")
        ?.click();
      await (row as EfLoginRow).updateComplete;
    }
    inspect(
      el,
      [
        ...filterBarTags,
        "h2",
        "ul",
        "li",
        "ef-login-row",
        "div",
        "span",
        "bdi",
        "button",
        "dl",
        "dt",
        "dd",
      ],
      [
        "username",
        "ip",
        "client",
        "url",
        "host",
        "agent",
        "provider",
        "mfa",
        "name",
      ],
    );
    const long = "u".repeat(10_000);
    const rowEl = document.createElement("ef-login-row");
    rowEl.hass = fake.hass;
    rowEl.detailed = true;
    rowEl.item = login({ url: long, user_agent: long, hostname: long });
    document.body.append(rowEl);
    await rowEl.updateComplete;
    rowEl.shadowRoot
      ?.querySelector<HTMLButtonElement>("button.toggle")
      ?.click();
    await rowEl.updateComplete;
    const values = [
      ...(rowEl.shadowRoot?.querySelectorAll(".details bdi") ?? []),
    ].filter((b) => b.getAttribute("title") === long);
    expect(values).toHaveLength(3);
    for (const value of values) {
      expect(value.textContent.length).toBe(65);
    }
  });

  it("audit", async () => {
    const fake = fakeApi({
      changes: {
        items: [
          change({
            actor_name: H("actor"),
            actor_ip: H("actorip"),
            target_name: H("target"),
            details: {
              changed: [H("changed")],
              data_keys_changed: [H("datakey")],
              options_keys_changed: [H("optkey")],
              field: H("field"),
              keys: [H("key")],
            },
          }),
        ],
        next_before_id: null,
      },
      users: { items: [] },
    });
    const el = await render("ef-audit", fake);
    inspect(
      el,
      [
        ...filterBarTags,
        "h2",
        "ul",
        "li",
        "ef-change-row",
        "div",
        "span",
        "bdi",
      ],
      [
        "actor",
        "actorip",
        "target",
        "changed",
        "datakey",
        "optkey",
        "field",
        "key",
      ],
    );
  });

  it("users with an expanded session list", async () => {
    const fake = fakeApi({
      users: {
        items: [
          {
            id: "u1",
            name: H("name"),
            username: H("uname"),
            is_admin: true,
            is_owner: true,
            is_active: true,
            local_only: true,
            system_generated: false,
            mfa_modules: ["totp"],
            last_login: login({ result: "success", ip: H("lastip") }),
            sessions: [
              {
                id: "s",
                client_name: H("sclient"),
                client_id: null,
                token_type: "normal",
                created_at: "2026-10-20T10:00:00+00:00",
                last_used_at: "2026-10-21T10:00:00+00:00",
                last_used_ip: H("sip"),
              },
            ],
          },
        ],
      },
    });
    const el = await render("ef-users", fake);
    el.shadowRoot?.querySelector("button")?.click();
    await (el as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    inspect(
      el,
      [
        "h2",
        "div",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "button",
        // Expand chevrons: the icon name is a constant, never data.
        "ha-icon",
        "bdi",
        "span",
      ],
      ["name", "uname", "lastip", "sclient", "sip"],
    );
    for (const icon of el.shadowRoot?.querySelectorAll("ha-icon") ?? []) {
      expect(["mdi:chevron-right", "mdi:chevron-down"]).toContain(
        icon.getAttribute("icon"),
      );
    }
  });

  it("settings", async () => {
    const s = status(H("attribution"));
    s.geo = {
      provider: "dbip_lite",
      db_date: null,
      last_error: H("geoerror"),
      attribution: H("attribution"),
    };
    s.collectors = [
      { name: H("collector"), enabled: false, reason: [H("reason")] },
    ];
    const el = await render("ef-settings", fakeApi({ status: s }));
    inspect(
      el,
      [
        "h2",
        "section",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "span",
        "p",
        "bdi",
        "button",
      ],
      ["geoerror", "attribution", "collector", "reason"],
    );
  });

  it("truncates long values and keeps the full text in title", async () => {
    const long = "a".repeat(10_000);
    const fake = fakeApi({
      users: {
        items: [
          {
            id: "u1",
            name: long,
            username: null,
            is_admin: false,
            is_owner: false,
            is_active: true,
            local_only: false,
            system_generated: false,
            mfa_modules: [],
            last_login: null,
            sessions: [],
          },
        ],
      },
    });
    const el = await render("ef-users", fake);
    const name = el.shadowRoot?.querySelector("button bdi");
    expect(name?.textContent.length).toBeLessThanOrEqual(65);
    expect(name?.getAttribute("title")).toBe(long);
  });
});
