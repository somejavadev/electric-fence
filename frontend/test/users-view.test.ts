import { afterEach, describe, expect, it } from "vitest";
import { EfLoginRow } from "../src/components/ef-login-row.ts";
import "../src/views/ef-users.ts";
import { viewStyles } from "../src/styles.ts";
import type { SessionItem, UserItem } from "../src/types.ts";
import { fakeApi, login } from "./helpers.ts";

// The Users tab follows Home Assistant's users page: a scannable list, with
// a user's sessions in a details dialog instead of a table nested in a row.

type Mounted = HTMLElement & { updateComplete: Promise<unknown> };

const session = (over: Partial<SessionItem> = {}): SessionItem => ({
  id: "s1",
  client_name: "Firefox",
  client_id: null,
  token_type: "normal",
  created_at: "2026-10-20T10:00:00+00:00",
  last_used_at: "2026-10-21T10:00:00+00:00",
  last_used_ip: "192.168.1.5",
  ...over,
});

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
  last_login: login({ result: "success", reason: "ok", ip: "10.0.0.7" }),
  sessions: [session()],
  ...over,
});

async function settle(el: Mounted): Promise<void> {
  for (let i = 0; i < 3; i += 1) {
    for (let j = 0; j < 8; j += 1) {
      await Promise.resolve();
    }
    await el.updateComplete;
  }
}

async function mountUsers(users: UserItem[]): Promise<Mounted> {
  const el = document.createElement("ef-users") as Mounted;
  (el as unknown as { hass: unknown }).hass = fakeApi({
    users: { items: users },
  }).hass;
  document.body.append(el);
  await settle(el);
  return el;
}

const root = (el: Mounted) => el.shadowRoot as ShadowRoot;

afterEach(() => {
  document.body.replaceChildren();
});

describe("Users list", () => {
  it("shows one row per user in a card, with no tables", async () => {
    const el = await mountUsers([user(), user({ id: "u2", name: "Bo" })]);
    expect(root(el).querySelector("table")).toBeNull();
    const rows = [...root(el).querySelectorAll(".card li button.user")];
    expect(rows).toHaveLength(2);
    expect(rows[0]?.getAttribute("aria-haspopup")).toBe("dialog");
  });

  it("puts the username and last login on a second line", async () => {
    const el = await mountUsers([user()]);
    const secondary = root(el).querySelector("button.user .secondary");
    expect(secondary?.textContent).toMatch(/anna/);
    expect(secondary?.textContent).toMatch(/10\.0\.0\.7/);
  });

  it("says never for a user who has not logged in", async () => {
    const el = await mountUsers([user({ last_login: null })]);
    expect(
      root(el).querySelector("button.user .secondary")?.textContent,
    ).toMatch(/Never logged in/);
  });

  it("keeps sessions out of the list until a user is opened", async () => {
    const el = await mountUsers([user()]);
    expect(root(el).querySelector("dialog")?.open ?? false).toBe(false);
    expect(root(el).querySelector(".card")?.textContent).not.toContain(
      "Firefox",
    );
  });
});

describe("User details dialog", () => {
  async function open(u: UserItem): Promise<Mounted> {
    const el = await mountUsers([u]);
    root(el).querySelector<HTMLButtonElement>("button.user")?.click();
    await settle(el);
    return el;
  }

  it("opens on click, titled with the user's name", async () => {
    const el = await open(user());
    const dialog = root(el).querySelector("dialog");
    expect(dialog?.open).toBe(true);
    expect(dialog?.querySelector("h3")?.textContent).toContain("Anna");
    expect(dialog?.getAttribute("aria-labelledby")).toBeTruthy();
  });

  it("lists sessions as rows, not a table", async () => {
    const el = await open(
      user({
        sessions: [
          session(),
          session({ id: "s2", client_name: null, client_id: "https://app/" }),
        ],
      }),
    );
    const dialog = root(el).querySelector("dialog") as HTMLDialogElement;
    expect(dialog.querySelector("table")).toBeNull();
    const items = [...dialog.querySelectorAll("li.session")];
    expect(items).toHaveLength(2);
    expect(items[0]?.textContent).toContain("Firefox");
    expect(items[0]?.textContent).toContain("192.168.1.5");
    expect(items[1]?.textContent).toContain("https://app/");
  });

  it.each([
    ["normal", "Normal"],
    ["long_lived_access_token", "Long-lived access token"],
    ["system", "System"],
    ["something_new", "something_new"],
  ])("names token type %s as %s", async (type, label) => {
    const el = await open(user({ sessions: [session({ token_type: type })] }));
    expect(root(el).querySelector("li.session")?.textContent).toContain(label);
  });

  it("does not repeat the Last login heading in its line", async () => {
    const el = await open(user());
    const line = root(el).querySelector("dialog p.last-login")?.textContent;
    expect(line).toMatch(/10\.0\.0\.7/);
    expect(line?.trim()).not.toMatch(/^Last login/);
  });

  it("starts focus on the dialog content, not the close button", async () => {
    const el = await open(user());
    const start = root(el).querySelector("dialog [autofocus]");
    expect(start?.getAttribute("tabindex")).toBe("-1");
    expect(start?.classList.contains("close")).toBe(false);
  });

  it("says when a user has no sessions", async () => {
    const el = await open(user({ sessions: [] }));
    expect(root(el).querySelector("dialog")?.textContent).toContain(
      "No sessions",
    );
  });

  it("has a close button and no other actions", async () => {
    const el = await open(user());
    const dialog = root(el).querySelector("dialog") as HTMLDialogElement;
    const buttons = [...dialog.querySelectorAll("button")];
    expect(buttons).toHaveLength(1);
    buttons[0]?.click();
    await settle(el);
    expect(dialog.open).toBe(false);
  });
});

describe("tables wrap words, not letters", () => {
  it("does not let table cells break inside words", () => {
    expect(viewStyles.cssText).not.toMatch(/overflow-wrap\s*:\s*anywhere/);
  });
});

describe("Activity details use sentence case", () => {
  async function details(over: Parameters<typeof login>[0]) {
    const row = document.createElement("ef-login-row") as EfLoginRow;
    row.hass = fakeApi({}).hass;
    row.item = login(over);
    row.detailed = true;
    document.body.append(row);
    await row.updateComplete;
    row.shadowRoot?.querySelector<HTMLButtonElement>("button")?.click();
    await row.updateComplete;
    return row.shadowRoot?.querySelector("dl")?.textContent ?? "";
  }

  it("capitalises None and Confirmed", async () => {
    const text = await details({
      result: "success",
      reason: "ok",
      confirmed: true,
      hostname: null,
    });
    expect(text).toMatch(/\bNone\b/);
    expect(text).toMatch(/\bConfirmed\b[\s\S]*\bConfirmed\b/);
    expect(text).not.toMatch(/\bnone\b/);
  });

  it.each([
    ["homeassistant", "Home Assistant"],
    ["trusted_networks", "Trusted networks"],
    ["command_line", "Command line"],
    ["my_custom", "my_custom"],
  ])("names provider %s as %s", async (provider, label) => {
    const text = await details({ result: "success", reason: "ok", provider });
    expect(text).toContain(label);
  });
});
