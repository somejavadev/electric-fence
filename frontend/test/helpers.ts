import type {
  AttackItem,
  BanItem,
  ChangeItem,
  HomeAssistant,
  LoginItem,
  Overview,
  Status,
  SubscribeEvent,
} from "../src/types.ts";

export function login(over: Partial<LoginItem> = {}): LoginItem {
  return {
    id: 1,
    ts: "2026-10-24T09:15:02+00:00",
    result: "failed",
    reason: "wrong_password",
    username_typed: "admin",
    user_exists: "yes",
    user_id: null,
    user_name: null,
    provider: null,
    ip: "203.0.113.7",
    country_code: "RU",
    hostname: null,
    source: "remote",
    client_id: "https://ha.example/",
    client_name: "Firefox",
    user_agent: null,
    url: null,
    mfa_module: null,
    confirmed: false,
    count: 1,
    new_country: false,
    ...over,
  };
}

export function change(over: Partial<ChangeItem> = {}): ChangeItem {
  return {
    id: 1,
    ts: "2026-10-24T08:00:00+00:00",
    kind: "config_entry",
    action: "added",
    target_id: "abc",
    target_name: "Tuya",
    details: {},
    actor_user_id: "u1",
    actor_name: "Anna",
    actor_ip: "192.168.1.5",
    ...over,
  };
}

export function overview(over: Partial<Overview> = {}): Overview {
  return {
    failed_24h: { count: 17, items: [login()] },
    changes_7d: { count: 3, items: [change()] },
    recent_success: {
      items: [login({ result: "success", reason: "ok", mfa_module: "totp" })],
    },
    posture: [
      { id: "admins_without_mfa", level: "ok", params: { users: [] } },
      {
        id: "auto_ban_off",
        level: "warning",
        params: { threshold: null, electric_fence: false },
      },
    ],
    protection: { available: true, active_bans: 2, bans_24h: 5, attacks: [] },
    ...over,
  };
}

export const status = (attribution: string | null = null): Status => ({
  version: "0.1.0",
  settings: {
    notify_failed_login: true,
    ha_login_notification: "replace",
    retention_days: 90,
    geoip_provider: "dbip_lite",
    maxmind_account_id: null,
    reverse_dns: false,
    record_values: false,
    auto_ban: true,
    ban_threshold: 5,
    ban_window_minutes: 15,
    ban_hours: 24,
    ban_repeat_offenders: false,
    username_protection: true,
    attack_threshold: 10,
    attack_min_ips: 3,
    attack_window_minutes: 15,
    notify_ip_banned: true,
    notify_username_attack: true,
    notify_new_country: true,
    notify_ban_ended: false,
    mask_ips: false,
    notify_targets: [],
    maxmind_license_key_set: false,
  },
  geo: {
    provider: "dbip_lite",
    db_date: null,
    last_error: null,
    attribution,
  },
  collectors: [],
  worker: { queued: 0, dropped: 0, storage_errors: 0 },
});

export function ban(over: Partial<BanItem> = {}): BanItem {
  return {
    ip: "203.0.113.9",
    source: "electric_fence",
    banned_at: "2026-10-24T09:00:00+00:00",
    expires_at: "2026-10-25T09:00:00+00:00",
    failures: 5,
    ban_number: 1,
    country_code: "RU",
    hostname: "scanner.example.net",
    ended_at: null,
    end_reason: null,
    ended_by_name: null,
    ...over,
  };
}

export function attack(over: Partial<AttackItem> = {}): AttackItem {
  return {
    username: "admin",
    started_at: "2026-10-24T09:00:00+00:00",
    ended_at: null,
    failures: 14,
    ip_count: 4,
    countries: ["RU", "CN"],
    end_reason: null,
    active: true,
    ...over,
  };
}

export interface FakeHass {
  hass: HomeAssistant;
  /** Replace what `electric_fence/overview` answers next. */
  setOverview(value: Overview): void;
  push(event: SubscribeEvent): void;
  calls: string[];
  unsubscribed: () => number;
  subscriptions: () => number;
}

export function fakeHass(
  first: Overview,
  attribution: string | null = null,
): FakeHass {
  let current = first;
  let callback: ((event: SubscribeEvent) => void) | undefined;
  let unsubs = 0;
  let subs = 0;
  const calls: string[] = [];
  const hass = {
    locale: { language: "en" },
    config: { time_zone: "UTC" },
    language: "en",
    connection: {
      sendMessagePromise: (message: { type: string }) => {
        calls.push(message.type);
        if (message.type === "electric_fence/overview") {
          return Promise.resolve(current);
        }
        if (message.type === "electric_fence/status") {
          return Promise.resolve(status(attribution));
        }
        return Promise.reject(new Error(`unexpected ${message.type}`));
      },
      subscribeMessage: (cb: (event: SubscribeEvent) => void) => {
        callback = cb;
        subs += 1;
        return Promise.resolve(() => {
          unsubs += 1;
        });
      },
    },
  } as unknown as HomeAssistant;
  return {
    hass,
    setOverview: (value) => {
      current = value;
    },
    push: (event) => callback?.(event),
    calls,
    unsubscribed: () => unsubs,
    subscriptions: () => subs,
  };
}

export interface FakeApi {
  hass: HomeAssistant;
  push(event: SubscribeEvent): void;
  /** Every message sent, in order. */
  sent: ({ type: string } & Record<string, unknown>)[];
  of(type: string): Record<string, unknown>[];
  subscriptions: () => number;
}

/** A hass whose commands are answered by `answers[type]` (value or function). */
export function fakeApi(
  answers: Record<
    string,
    unknown | ((message: Record<string, unknown>) => unknown)
  >,
): FakeApi {
  let callback: ((event: SubscribeEvent) => void) | undefined;
  let subs = 0;
  const sent: FakeApi["sent"] = [];
  const hass = {
    locale: { language: "en" },
    config: { time_zone: "UTC" },
    language: "en",
    connection: {
      sendMessagePromise: (
        message: { type: string } & Record<string, unknown>,
      ) => {
        sent.push(message);
        const name = message.type.replace("electric_fence/", "");
        if (!(name in answers)) {
          return Promise.reject(new Error(`unexpected ${message.type}`));
        }
        const answer = answers[name];
        return Promise.resolve(
          typeof answer === "function" ? answer(message) : answer,
        );
      },
      subscribeMessage: (cb: (event: SubscribeEvent) => void) => {
        callback = cb;
        subs += 1;
        return Promise.resolve(() => undefined);
      },
    },
  } as unknown as HomeAssistant;
  return {
    hass,
    push: (event) => callback?.(event),
    sent,
    of: (type) => sent.filter((m) => m.type === `electric_fence/${type}`),
    subscriptions: () => subs,
  };
}
