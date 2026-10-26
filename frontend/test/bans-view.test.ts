import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/views/ef-bans.ts";
import { banExpiry } from "../src/strings.ts";
import type { BanItem } from "../src/types.ts";
import { ban, fakeApi } from "./helpers.ts";
import type { FakeApi } from "./helpers.ts";

// The Bans tab follows the Users view: a list, a details dialog, and an
// Unban that asks first in a dialog styled like Home Assistant's.

type Mounted = HTMLElement & { updateComplete: Promise<unknown> };

const HOUR = 3_600_000;
const inHours = (h: number) => new Date(Date.now() + h * HOUR).toISOString();

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

const page = (
  items: BanItem[],
  available = true,
  reason: string | null = null,
) => ({
  available,
  reason,
  items,
  next_before_id: null,
});

async function mount(api: FakeApi): Promise<Mounted> {
  const el = document.createElement("ef-bans") as Mounted;
  (el as unknown as { hass: unknown }).hass = api.hass;
  document.body.append(el);
  await settle(el);
  return el;
}

const root = (el: Mounted) => el.shadowRoot as ShadowRoot;

function bar(el: Mounted): ShadowRoot {
  return root(el).querySelector("ef-filter-bar")?.shadowRoot as ShadowRoot;
}

function choose(el: Mounted, name: string, value: string): void {
  const control = bar(el).querySelector<HTMLSelectElement>(
    `[name=${name}]`,
  ) as HTMLSelectElement;
  control.value = value;
  control.dispatchEvent(new Event("change", { bubbles: true }));
}

const confirmRoot = (el: Mounted) =>
  root(el).querySelector("ef-confirm-dialog")?.shadowRoot ?? null;

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("ef-bans", () => {
  it("bans list renders rows with flag and tag", async () => {
    const fake = fakeApi({
      bans: page([
        ban({ expires_at: inHours(3) }),
        ban({
          ip: "198.51.100.4",
          source: "other",
          expires_at: null,
          failures: null,
          ban_number: null,
          country_code: null,
          hostname: null,
        }),
      ]),
    });
    const el = await mount(fake);
    expect(fake.of("bans")[0]).toMatchObject({ state: "active" });
    const rows = [...root(el).querySelectorAll(".card li button.ban")];
    expect(rows).toHaveLength(2);
    expect(rows[0]?.getAttribute("aria-haspopup")).toBe("dialog");
    const first = rows[0] as HTMLElement;
    expect(
      (first.querySelector("ef-flag") as unknown as { code: string }).code,
    ).toBe("RU");
    expect(first.textContent).toContain("203.0.113.9");
    expect(first.textContent).toContain("RU");
    expect(first.querySelector(".tag")?.textContent?.trim()).toBe(
      "Electric Fence",
    );
    expect(first.textContent).toContain("Expires in 3 h");
    const second = rows[1] as HTMLElement;
    expect(second.querySelector(".tag")?.textContent?.trim()).toBe("Other");
    expect(second.textContent).toContain("Never");
    expect(second.textContent).toContain("Unknown country");
  });

  it("filters by source on the client", async () => {
    const fake = fakeApi({
      bans: page([ban(), ban({ ip: "198.51.100.4", source: "other" })]),
    });
    const el = await mount(fake);
    choose(el, "source", "other");
    await settle(el);
    const rows = [...root(el).querySelectorAll("button.ban")];
    expect(rows).toHaveLength(1);
    expect(rows[0]?.textContent).toContain("198.51.100.4");
    expect(fake.of("bans")).toHaveLength(1);
  });

  it("history filter fetches state history", async () => {
    const fake = fakeApi({
      bans: (m: Record<string, unknown>) =>
        m["state"] === "history"
          ? page([
              ban({
                ended_at: "2026-10-24T12:00:00+00:00",
                end_reason: "unbanned",
                ended_by_name: "Anna",
              }),
            ])
          : page([]),
    });
    const el = await mount(fake);
    choose(el, "state", "history");
    await settle(el);
    expect(fake.of("bans").at(-1)).toMatchObject({ state: "history" });
    const row = root(el).querySelector("button.ban");
    expect(row?.textContent).toContain("Ended");
    // Ended bans have nothing to lift.
    (row as HTMLButtonElement).click();
    await settle(el);
    const dialog = root(el).querySelector("dialog") as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(dialog.textContent).toContain("Unbanned by Anna");
    expect(dialog.querySelector("button.unban")).toBeNull();
  });

  it("loads more history with before_id", async () => {
    const fake = fakeApi({
      bans: (m: Record<string, unknown>) =>
        m["before_id"] === undefined
          ? {
              ...page([ban({ ended_at: "2026-10-24T12:00:00+00:00" })]),
              next_before_id: 7,
            }
          : page([
              ban({
                ip: "198.51.100.4",
                ended_at: "2026-10-23T12:00:00+00:00",
              }),
            ]),
    });
    const el = await mount(fake);
    choose(el, "state", "history");
    await settle(el);
    const more = [...root(el).querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "Load more",
    );
    more?.click();
    await settle(el);
    expect(fake.of("bans").at(-1)).toMatchObject({
      state: "history",
      before_id: 7,
    });
    expect(root(el).querySelectorAll("button.ban")).toHaveLength(2);
  });

  it("shows every detail in the dialog", async () => {
    const fake = fakeApi({
      bans: page([ban({ failures: 7, ban_number: 2 })]),
    });
    const el = await mount(fake);
    root(el).querySelector<HTMLButtonElement>("button.ban")?.click();
    await settle(el);
    const dialog = root(el).querySelector("dialog") as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(dialog.getAttribute("aria-labelledby")).toBeTruthy();
    expect(dialog.querySelector("h3")?.textContent).toContain("203.0.113.9");
    const text = dialog.textContent ?? "";
    expect(text).toMatch(/Country\s*RU/);
    expect(text).toMatch(/Host name\s*scanner\.example\.net/);
    expect(text).toMatch(/Failed attempts\s*7/);
    expect(text).toMatch(/Ban number\s*2/);
    expect(text).toMatch(/Banned at\s*Oct 24, 2026/);
    expect(text).toMatch(/Expires\s*Oct 25, 2026/);
    expect(dialog.querySelector("ef-flag")).not.toBeNull();
    const start = dialog.querySelector("[autofocus]");
    expect(start?.getAttribute("tabindex")).toBe("-1");
  });

  it("unban asks then calls unban", async () => {
    let active = [ban()];
    const fake = fakeApi({
      bans: () => page(active),
      unban: () => {
        active = [];
        return { unbanned: true };
      },
    });
    const el = await mount(fake);
    root(el).querySelector<HTMLButtonElement>("button.ban")?.click();
    await settle(el);
    root(el).querySelector<HTMLButtonElement>("dialog button.unban")?.click();
    await settle(el);
    expect(fake.of("unban")).toHaveLength(0);
    const confirm = confirmRoot(el) as ShadowRoot;
    const dialog = confirm.querySelector("dialog") as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(dialog.textContent?.replace(/\s+/g, " ")).toContain(
      "Unban 203.0.113.9? It can log in again straight away.",
    );
    const before = fake.of("bans").length;
    confirm.querySelector<HTMLButtonElement>("button.confirm")?.click();
    await settle(el);
    expect(fake.of("unban")).toEqual([
      { type: "electric_fence/unban", ip: "203.0.113.9" },
    ]);
    expect(fake.of("bans").length).toBeGreaterThan(before);
    expect(root(el).querySelector("ef-confirm-dialog")).toBeNull();
    expect(root(el).querySelectorAll("button.ban")).toHaveLength(0);
  });

  it("cancelling the confirmation sends nothing", async () => {
    const fake = fakeApi({ bans: page([ban()]), unban: { unbanned: true } });
    const el = await mount(fake);
    root(el).querySelector<HTMLButtonElement>("button.ban")?.click();
    await settle(el);
    root(el).querySelector<HTMLButtonElement>("dialog button.unban")?.click();
    await settle(el);
    confirmRoot(el)?.querySelector<HTMLButtonElement>("button.cancel")?.click();
    await settle(el);
    expect(fake.of("unban")).toHaveLength(0);
    expect(root(el).querySelector("ef-confirm-dialog")).toBeNull();
    expect(root(el).querySelector<HTMLDialogElement>("dialog")?.open).toBe(
      true,
    );
  });

  it("keeps the confirmation open with an error when unban fails", async () => {
    const fake = fakeApi({ bans: page([ban()]) });
    const el = await mount(fake);
    root(el).querySelector<HTMLButtonElement>("button.ban")?.click();
    await settle(el);
    root(el).querySelector<HTMLButtonElement>("dialog button.unban")?.click();
    await settle(el);
    confirmRoot(el)
      ?.querySelector<HTMLButtonElement>("button.confirm")
      ?.click();
    await settle(el);
    const confirm = confirmRoot(el) as ShadowRoot;
    expect(confirm.querySelector("dialog")?.open).toBe(true);
    expect(confirm.querySelector("[role=alert]")?.textContent).toContain(
      "Could not unban",
    );
    expect(
      confirm.querySelector<HTMLButtonElement>("button.confirm")?.disabled,
    ).toBe(false);
  });

  it("offers no Unban while bans cannot be applied", async () => {
    const fake = fakeApi({ bans: page([ban()], false, "backend_error") });
    const el = await mount(fake);
    root(el).querySelector<HTMLButtonElement>("button.ban")?.click();
    await settle(el);
    expect(root(el).querySelector("dialog")?.open).toBe(true);
    expect(root(el).querySelector("dialog button.unban")).toBeNull();
  });

  it("sends one unban for a double click on confirm", async () => {
    const fake = fakeApi({ bans: page([ban()]), unban: { unbanned: true } });
    const el = await mount(fake);
    root(el).querySelector<HTMLButtonElement>("button.ban")?.click();
    await settle(el);
    root(el).querySelector<HTMLButtonElement>("dialog button.unban")?.click();
    await settle(el);
    const confirm =
      confirmRoot(el)?.querySelector<HTMLButtonElement>("button.confirm");
    confirm?.click();
    confirm?.click();
    await settle(el);
    expect(fake.of("unban")).toHaveLength(1);
  });

  it("unavailable empty state", async () => {
    const fake = fakeApi({ bans: page([], false, "ip_ban_disabled") });
    const el = await mount(fake);
    expect(root(el).querySelector(".card .empty")?.textContent).toContain(
      "IP banning is off in Home Assistant's HTTP settings (ip_ban_enabled).",
    );
    expect(root(el).querySelector("button.ban")).toBeNull();
  });

  it("says when there are no active bans", async () => {
    const el = await mount(fakeApi({ bans: page([]) }));
    expect(root(el).querySelector(".card .empty")?.textContent).toContain(
      "No active bans",
    );
  });

  it("refetches throttled on a live push", async () => {
    const fake = fakeApi({ bans: page([]) });
    await mount(fake);
    vi.useFakeTimers();
    fake.push({
      type: "change",
      item: {
        id: null,
        ts: "2026-10-24T09:00:00+00:00",
        kind: "login_protection",
        action: "banned",
        target_id: null,
        target_name: null,
        details: {},
        actor_user_id: null,
        actor_name: null,
        actor_ip: null,
      },
    });
    await vi.advanceTimersByTimeAsync(1000);
    expect(fake.of("bans")).toHaveLength(2);
  });
});

describe("banExpiry", () => {
  const now = Date.parse("2026-10-24T09:00:00Z");
  const at = (ms: number) => new Date(now + ms).toISOString();

  it.each([
    [{ expires_at: null, ended_at: null }, "Never"],
    [{ expires_at: at(3 * HOUR), ended_at: null }, "Expires in 3 h"],
    [{ expires_at: at(45 * 60_000), ended_at: null }, "Expires in 45 min"],
    [{ expires_at: at(20_000), ended_at: null }, "Expires in 1 min"],
    [{ expires_at: at(-5_000), ended_at: null }, "Expires in 1 min"],
    [{ expires_at: at(72 * HOUR), ended_at: null }, "Expires in 3 d"],
    [{ expires_at: at(59.5 * 60_000), ended_at: null }, "Expires in 1 h"],
    [{ expires_at: at(59 * 60_000), ended_at: null }, "Expires in 59 min"],
    [{ expires_at: at(3 * HOUR), ended_at: at(-HOUR) }, "Ended"],
  ])("%o reads %s", (b, label) => {
    expect(banExpiry(b, now)).toBe(label);
  });
});
