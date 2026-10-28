import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type { EfChangeRow } from "../src/components/ef-change-row.ts";
import "../src/components/ef-change-row.ts";
import "../src/components/ef-login-row.ts";
import "../src/electric-fence-panel.ts";
import {
  maskIp,
  showClient,
  maskingIps,
  setMaskIps,
  showHost,
  showIp,
} from "../src/mask.ts";
import { changeSentence, postureText } from "../src/strings.ts";
import type { Status } from "../src/types.ts";
import "../src/views/ef-bans.ts";
import "../src/views/ef-users.ts";
import { ban, change, fakeApi, login, overview, status } from "./helpers.ts";
import type { FakeApi } from "./helpers.ts";

// "Mask IP addresses in the UI": only what the panel shows changes. The
// server's answers and the panel's own requests keep the full address.

type Mounted = HTMLElement & { updateComplete: Promise<unknown> };

const squash = (text: string | null | undefined) =>
  (text ?? "").replace(/\s+/g, " ").trim();

async function settle(el: Mounted): Promise<void> {
  for (let i = 0; i < 4; i += 1) {
    for (let j = 0; j < 8; j += 1) {
      await Promise.resolve();
    }
    await el.updateComplete;
    const confirm = el.shadowRoot?.querySelector("ef-confirm-dialog") as
      Mounted | null | undefined;
    await confirm?.updateComplete;
  }
}

beforeAll(() => {
  if (customElements.get("ha-menu-button") === undefined) {
    customElements.define("ha-menu-button", class extends HTMLElement {});
  }
});

afterEach(() => {
  setMaskIps(false);
  document.body.replaceChildren();
});

describe("maskIp", () => {
  it.each([
    ["203.0.113.42", "203.x.x.x"],
    ["10.0.0.1", "10.x.x.x"],
    ["::ffff:198.51.100.7", "198.x.x.x"],
    ["2001:db8:85a3::8a2e:370:7334", "2001:db8:x:x:x:x:x:x"],
    ["2001:db8:85a3:0:0:8a2e:370:7334", "2001:db8:x:x:x:x:x:x"],
    ["fe80::1%eth0", "fe80:0:x:x:x:x:x:x"],
    ["::1", "0:0:x:x:x:x:x:x"],
    ["::", "0:0:x:x:x:x:x:x"],
  ])("masks %s as %s", (ip, masked) => {
    expect(maskIp(ip)).toBe(masked);
  });

  it.each([
    "",
    "unknown",
    "1.2.3",
    "1:2:3",
    "1::2::3",
    "gggg::1",
    "1:2:3:4:5:6:7:8:9",
  ])("hides %j, which is not an address", (value) => {
    expect(maskIp(value)).toBe("hidden");
  });

  it("changes nothing while the option is off", () => {
    expect(showIp("203.0.113.42")).toBe("203.0.113.42");
    expect(showHost("c-203-0-113-42.isp.example")).toBe(
      "c-203-0-113-42.isp.example",
    );
    setMaskIps(true);
    expect(showIp("203.0.113.42")).toBe("203.x.x.x");
    expect(showHost("c-203-0-113-42.isp.example")).toBe("hidden");
  });
});

describe("showClient", () => {
  it.each([
    ["https://ha.example.com/", "https://hidden/"],
    ["http://127.0.0.1:8123/", "http://hidden/"],
    // The path can name the server too (a reverse proxy's sub-path).
    ["http://[2001:db8::1]:8123/lovelace", "http://hidden/"],
    ["https://proxy.example.com/ha-smith/", "https://hidden/"],
    ["https://home-assistant.io/android", "https://home-assistant.io/android"],
    ["https://home-assistant.io/iOS", "https://home-assistant.io/iOS"],
    ["not a url", "hidden"],
  ])("masks %s as %s", (clientId, masked) => {
    expect(showClient(clientId)).toBe(clientId);
    setMaskIps(true);
    expect(showClient(clientId)).toBe(masked);
  });

  it("does not trust a host that only ends like the apps' one", () => {
    setMaskIps(true);
    expect(showClient("https://evilhome-assistant.io/")).toBe(
      "https://hidden/",
    );
  });
});

describe("masked screens", () => {
  it("masks the client URL of a successful login", async () => {
    setMaskIps(true);
    const el = document.createElement("ef-login-row");
    el.hass = fakeApi({}).hass;
    el.variant = "success";
    el.item = login({
      result: "success",
      reason: "ok",
      client_name: null,
      client_id: "https://ha.example.com/",
    });
    document.body.append(el);
    await el.updateComplete;
    const client = el.shadowRoot?.querySelector(".client");
    expect(client?.textContent).toBe("https://hidden/");
    expect(client?.getAttribute("title")).toBe("https://hidden/");
  });

  it("masks session client URLs and addresses in the Users tab", async () => {
    setMaskIps(true);
    const session = {
      id: "s1",
      client_name: null,
      client_id: "https://ha.example.com/",
      token_type: "normal",
      created_at: "2026-10-20T10:00:00+00:00",
      last_used_at: null,
      last_used_ip: "198.51.100.20",
    };
    const fake = fakeApi({
      users: {
        items: [
          {
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
            sessions: [session],
          },
        ],
      },
    });
    const el = document.createElement("ef-users") as Mounted;
    (el as unknown as { hass: unknown }).hass = fake.hass;
    document.body.append(el);
    await settle(el);
    const root = el.shadowRoot as ShadowRoot;
    root.querySelector<HTMLButtonElement>("button.user")?.click();
    await settle(el);
    const text = squash(root.textContent);
    expect(text).toContain("https://hidden/");
    expect(text).toContain("198.x.x.x");
    expect(text).toContain("203.x.x.x");
    expect(text).not.toContain("ha.example.com");
    expect(text).not.toContain("198.51");
    expect(text).not.toContain("203.0");
  });

  it("masks the login row's address and its host name", async () => {
    setMaskIps(true);
    const el = document.createElement("ef-login-row");
    el.hass = fakeApi({}).hass;
    el.variant = "failed";
    el.detailed = true;
    el.item = login({ hostname: "c-203-0-113-7.isp.example" });
    document.body.append(el);
    await el.updateComplete;
    const root = el.shadowRoot as ShadowRoot;
    expect(root.querySelector(".ip")?.textContent).toBe("203.x.x.x");
    root.querySelector<HTMLButtonElement>("button.toggle")?.click();
    await el.updateComplete;
    const details = root.querySelector(".details")?.textContent ?? "";
    expect(details).toContain("hidden");
    expect(root.textContent).not.toContain("113");
  });

  it("masks the actor's address and the ban sentence in the audit", async () => {
    setMaskIps(true);
    const item = change({
      kind: "ip_ban",
      action: "banned",
      target_id: "203.0.113.9",
      actor_ip: "198.51.100.3",
    });
    expect(changeSentence(item)).toBe("banned 203.x.x.x");
    const el = document.createElement("ef-change-row") as EfChangeRow;
    el.hass = fakeApi({}).hass;
    el.item = item;
    document.body.append(el);
    await el.updateComplete;
    const text = el.shadowRoot?.textContent ?? "";
    expect(text).toContain("198.x.x.x");
    expect(text).not.toContain("198.51");
    expect(text).not.toContain("203.0");
  });

  it("masks the address in the proxy warning", () => {
    setMaskIps(true);
    expect(
      postureText({
        id: "proxy_suspected",
        level: "warning",
        params: { ip: "172.30.32.1", users: 4, use_x_forwarded_for: null },
      }),
    ).toContain("(172.x.x.x)");
  });

  it("masks the Bans tab but unbans the full address", async () => {
    setMaskIps(true);
    const fake = fakeApi({
      bans: {
        available: true,
        reason: null,
        items: [ban()],
        next_before_id: null,
      },
      unban: { unbanned: true },
    });
    const el = document.createElement("ef-bans") as Mounted;
    (el as unknown as { hass: unknown }).hass = fake.hass;
    document.body.append(el);
    await settle(el);
    const root = el.shadowRoot as ShadowRoot;
    expect(squash(root.querySelector("button.ban")?.textContent)).toContain(
      "203.x.x.x",
    );
    root.querySelector<HTMLButtonElement>("button.ban")?.click();
    await settle(el);
    const dialog = root.querySelector("dialog") as HTMLDialogElement;
    expect(dialog.textContent).not.toContain("scanner.example.net");
    expect(dialog.textContent).not.toContain("203.0.113.9");
    dialog.querySelector<HTMLButtonElement>("button.unban")?.click();
    await settle(el);
    const confirm = root.querySelector("ef-confirm-dialog")
      ?.shadowRoot as ShadowRoot;
    expect(squash(confirm.textContent)).toContain("Unban 203.x.x.x?");
    confirm.querySelector<HTMLButtonElement>("button.confirm")?.click();
    await settle(el);
    expect(fake.of("unban")).toEqual([
      { type: "electric_fence/unban", ip: "203.0.113.9" },
    ]);
  });
});

describe("panel", () => {
  const withMask = (on: boolean): Status => {
    const s = status();
    return { ...s, settings: { ...s.settings, mask_ips: on } };
  };

  async function mount(fake: FakeApi): Promise<Mounted> {
    const el = document.createElement("electric-fence-panel");
    el.hass = fake.hass;
    el.route = { prefix: "/electric-fence", path: "/overview" };
    document.body.append(el);
    await settle(el);
    return el as Mounted;
  }

  const main = (el: Mounted) => el.shadowRoot?.querySelector("main");

  it("shows no tab until the option is read, then masks", async () => {
    let answer: (value: Status) => void = () => undefined;
    const pending = new Promise<Status>((resolve) => {
      answer = resolve;
    });
    const fake = fakeApi({ status: () => pending, overview: overview() });
    const el = await mount(fake);
    expect(main(el)?.children).toHaveLength(0);
    answer(withMask(true));
    await settle(el);
    expect(maskingIps()).toBe(true);
    expect(main(el)?.querySelector("ef-overview")).not.toBeNull();
  });

  it("reads the option again when it is opened again", async () => {
    let on = true;
    const fake = fakeApi({ status: () => withMask(on), overview: overview() });
    const el = await mount(fake);
    expect(maskingIps()).toBe(true);
    const first = main(el)?.querySelector("ef-overview");
    el.remove();
    on = false;
    document.body.append(el);
    await settle(el);
    expect(maskingIps()).toBe(false);
    expect(fake.of("status").length).toBeGreaterThanOrEqual(2);
    // A new tab element, so nothing masked differently stays on screen.
    expect(main(el)?.querySelector("ef-overview")).not.toBe(first);
  });

  it("shows the tabs unmasked when the status cannot be read", async () => {
    setMaskIps(false);
    const fake = fakeApi({ overview: overview() });
    const el = await mount(fake);
    expect(maskingIps()).toBe(false);
    expect(main(el)?.querySelector("ef-overview")).not.toBeNull();
  });
});
