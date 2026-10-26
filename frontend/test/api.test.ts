import { describe, expect, it, vi } from "vitest";
import {
  clearCountries,
  endSlowdown,
  fetchAttacks,
  fetchBans,
  fetchChanges,
  fetchLogins,
  fetchOverview,
  subscribe,
  unban,
} from "../src/api.ts";
import type { HomeAssistant, SubscribeEvent } from "../src/types.ts";

function fakeHass() {
  const sendMessagePromise = vi.fn().mockResolvedValue({});
  let sent: object = {};
  let pushed: (event: SubscribeEvent) => void = () => undefined;
  const unsub = vi.fn();
  const subscribeMessage = vi.fn(
    (cb: (event: SubscribeEvent) => void, msg: object) => {
      sent = msg;
      pushed = cb;
      return Promise.resolve(unsub);
    },
  );
  const hass = {
    connection: { sendMessagePromise, subscribeMessage },
  } as unknown as HomeAssistant;
  return {
    hass,
    sendMessagePromise,
    subscribeMessage,
    unsub,
    push: (e: SubscribeEvent) => pushed(e),
    sent: () => sent,
  };
}

describe("api", () => {
  it("prefixes the command type", async () => {
    const { hass, sendMessagePromise } = fakeHass();
    await fetchOverview(hass);
    expect(sendMessagePromise).toHaveBeenCalledWith({
      type: "electric_fence/overview",
    });
  });

  it("passes filters alongside the type", async () => {
    const { hass, sendMessagePromise } = fakeHass();
    await fetchLogins(hass, { result: "failed", limit: 10 });
    expect(sendMessagePromise).toHaveBeenCalledWith({
      type: "electric_fence/logins",
      result: "failed",
      limit: 10,
    });
  });

  it("does not let filter keys override the command type", async () => {
    const { hass, sendMessagePromise } = fakeHass();
    const hostile = { type: "config/auth/delete" } as unknown as object;
    await fetchChanges(hass, hostile);
    await fetchLogins(hass, hostile);
    for (const [message] of sendMessagePromise.mock.calls) {
      expect((message as { type: string }).type).toMatch(/^electric_fence\//);
    }
  });

  it("forwards subscription events and returns the unsubscribe", async () => {
    const { hass, unsub, push, sent } = fakeHass();
    const seen: SubscribeEvent[] = [];
    const off = await subscribe(hass, (e) => seen.push(e));
    expect(sent()).toEqual({
      type: "electric_fence/subscribe",
    });
    const login = {
      type: "login",
      item: { id: null },
    } as unknown as SubscribeEvent;
    push(login);
    expect(seen).toEqual([login]);
    expect(off).toBe(unsub);
  });

  it("surfaces an unloaded event", async () => {
    const { hass, push } = fakeHass();
    const seen: SubscribeEvent[] = [];
    await subscribe(hass, (e) => seen.push(e));
    push({ type: "unloaded" });
    expect(seen).toEqual([{ type: "unloaded" }]);
  });

  it("sends the protection commands with their parameters", async () => {
    const { hass, sendMessagePromise } = fakeHass();
    await fetchBans(hass, "active");
    await fetchBans(hass, "history", 9);
    await unban(hass, "203.0.113.9");
    await fetchAttacks(hass, "active");
    await endSlowdown(hass, "admin");
    await clearCountries(hass);
    expect(sendMessagePromise.mock.calls.map(([m]) => m as object)).toEqual([
      { type: "electric_fence/bans", state: "active" },
      { type: "electric_fence/bans", state: "history", before_id: 9 },
      { type: "electric_fence/unban", ip: "203.0.113.9" },
      { type: "electric_fence/attacks", state: "active" },
      { type: "electric_fence/end_slowdown", username: "admin" },
      { type: "electric_fence/clear_countries" },
    ]);
  });
});
