import { afterEach, describe, expect, it } from "vitest";
import "../src/components/ef-login-row.ts";
import { EfLoginRow } from "../src/components/ef-login-row.ts";
import type { HomeAssistant } from "../src/types.ts";
import "../src/views/ef-overview.ts";
import { change, fakeHass, login, overview } from "./helpers.ts";

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

    const shown = allShadowElements(el.shadowRoot as ShadowRoot)
      .map((e) => e.textContent)
      .join("\n");
    // Rendered fields (hostname, user agent and the success IP are never shown).
    for (const label of labels.filter(
      (l) => l !== "host" && l !== "successip",
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
