import { truncate } from "./format.ts";
import type { ChangeItem, PostureItem } from "./types.ts";

/** Limit a value the server or a user supplied before it joins a sentence. */
const cut = (text: string): string => truncate(text, 64).short;

/**
 * Every sentence the Overview shows. English only for v0.1, kept in one place
 * so it can be translated later. Names and other user-controlled values are
 * inserted as text by the callers; nothing here becomes markup.
 */
export const strings = {
  cards: {
    failed: "Failed logins · 24h",
    changes: "Settings changes · 7d",
    success: "Recent successful logins",
    posture: "Posture",
  },
  empty: {
    failed: "No failed logins in the last 24 hours",
    changes: "No settings changes in the last 7 days",
    success: "No successful logins recorded yet",
    posture: "No checks to show",
  },
  loading: "Loading…",
  loadError: "Could not load this information from Home Assistant.",
  retry: "Try again",
  liveUnavailable:
    "Live updates are unavailable. The numbers shown may be out of date.",
  unloaded: "Electric Fence was unloaded or reloaded",
  unloadedHint:
    "Live updates have stopped. Try again once the integration is back.",
  postureProblems: (n: number): string =>
    n === 0 ? "All clear" : n === 1 ? "1 to check" : `${n} to check`,
  local: "local",
  unknownCountry: "unknown country",
  unknownIp: "unknown IP",
  unknownUser: "unknown user",
  unknownClient: "unknown client",
  system: "System",
  mfa: "Two-factor",
  noMfa: "No two-factor",
  repeated: (n: number): string => `×${n}`,
  unconfirmed: "Unconfirmed",
  details: {
    show: "Details",
    hide: "Hide details",
    url: "URL",
    hostname: "Host name",
    userAgent: "User agent",
    provider: "Provider",
    mfaModule: "Two-factor module",
    state: "Confirmed",
    confirmed: "Confirmed",
    unconfirmed: "Unconfirmed (no token issued)",
    none: "None",
    /** Home Assistant's auth providers; others show as-is. */
    providers: {
      homeassistant: "Home Assistant",
      trusted_networks: "Trusted networks",
      command_line: "Command line",
      legacy_api_password: "API password",
    } as Record<string, string>,
  },
  level: { ok: "OK", warning: "Warning", error: "Problem" },
  filters: {
    result: "Result",
    anyResult: "Any result",
    results: {
      success: "Success",
      failed: "Failed",
      blocked: "Blocked",
      failed_or_blocked: "Failed or blocked",
    },
    user: "User",
    anyUser: "Any user",
    username: "Typed username",
    ip: "IP address",
    country: "Country code",
    range: "Time range",
    kind: "Kind",
    anyKind: "Any kind",
    kinds: {
      config_entry: "Integrations",
      user: "Users",
      core_config: "Core settings",
      backup: "Backups",
      http: "HTTP",
      network: "Network",
      analytics: "Analytics",
      logger: "Logging",
      recorder: "Recorder",
    },
    actor: "Actor",
    anyActor: "Anyone",
    showSystem: "Show system changes",
    ranges: {
      "24h": "Last 24 hours",
      "7d": "Last 7 days",
      "30d": "Last 30 days",
      "90d": "Last 90 days",
      all: "All time",
    },
  },
  activity: { heading: "Activity", empty: "No logins match these filters" },
  audit: {
    heading: "Audit",
    empty: "No settings changes match these filters",
    changed: "changed",
    dataKeys: "data keys",
    optionKeys: "option keys",
    field: "field",
    keys: "keys",
    changedLabel: "Changed",
    optionPrefix: "Option",
    dataKeysLabel: "Data keys",
    optionKeysLabel: "Option keys",
    andMore: (n: number): string => `and ${n} more`,
    /** Core setting names that read better than their key. */
    settingNames: { location_name: "Home name" } as Record<string, string>,
    hiddenValue: "changed (hidden)",
    noValue: "—",
  },
  loadMore: "Load more",
  users: {
    heading: "Users",
    empty: "No users to show",
    admin: "Admin",
    owner: "Owner",
    active: "Active",
    inactive: "Inactive",
    localOnly: "Local only",
    neverLoggedIn: "Never logged in",
    lastLoginAt: (when: string): string => `Last login ${when}`,
    open: "Show details",
    close: "Close",
    lastLoginHeading: "Last login",
    sessionsHeading: (n: number): string => `Sessions (${n})`,
    lastUsedAt: (when: string): string => `last used ${when}`,
    neverUsed: "never used",
    createdAt: (when: string): string => `created ${when}`,
    /** Home Assistant's refresh token types; others show as-is. */
    tokenTypes: {
      normal: "Normal",
      long_lived_access_token: "Long-lived access token",
      system: "System",
    } as Record<string, string>,
    noSessions: "No sessions",
  },
  settings: {
    heading: "Settings",
    geoip: "Country lookup",
    provider: "Provider",
    providers: {
      dbip_lite: "DB-IP Lite",
      maxmind: "MaxMind",
      none: "None",
    },
    notConfigured: "Country lookup is not set up",
    databaseDate: "Database date",
    unknown: "Unknown",
    lastError: "Last error",
    none: "None",
    collectors: "Collectors",
    collector: "Collector",
    state: "State",
    on: "On",
    off: "Off",
    collectorNames: {
      login: "Logins",
      auth_failures: "Failed authentication",
      attribution: "Who made changes",
      config_entries: "Integrations",
      users: "Users",
      core_config: "Core settings",
    } as Record<string, string>,
    reasons: "Reasons",
    worker: "Worker",
    queued: "Queued",
    dropped: "Dropped",
    storageErrors: "Storage errors",
    open: "Open settings",
    openHint:
      "Options are changed in Home Assistant's integration settings, not here.",
  },
} as const;

const SYSTEM_SENTENCES: Record<string, Record<string, string>> = {
  backup: {
    "": "changed backup settings",
    removed: "deleted a backup",
    restore_requested: "requested a backup restore",
  },
  http: {
    "": "changed HTTP settings",
    withdrawn: "withdrew pending HTTP settings",
    confirmed: "confirmed HTTP settings",
  },
  network: { "": "changed network settings" },
  analytics: { "": "changed analytics settings" },
  logger: { "": "changed log levels" },
  recorder: {
    "": "changed recorder settings",
    disabled: "disabled the recorder",
    enabled: "enabled the recorder",
    purged: "purged the recorder",
  },
};

/** Sentence for the system settings kinds; unknown kinds read "changed x". */
function systemSentence(item: ChangeItem): string | undefined {
  const known = ["config_entry", "user", "core_config"];
  if (known.includes(item.kind)) {
    return undefined;
  }
  const own = Object.hasOwn(SYSTEM_SENTENCES, item.kind)
    ? SYSTEM_SENTENCES[item.kind]
    : undefined;
  if (own === undefined) {
    return `changed ${cut(item.kind)}`;
  }
  return (Object.hasOwn(own, item.action) ? own[item.action] : own[""]) ?? "";
}

/** Sentence for a settings change, such as "added integration Tuya". */
export function changeSentence(item: ChangeItem): string {
  const fixed = systemSentence(item);
  if (fixed !== undefined) {
    return fixed;
  }
  if (item.kind === "core_config") {
    const keys = item.details["keys"] ?? item.details["changed"];
    const list = Array.isArray(keys)
      ? keys.filter((key): key is string => typeof key === "string")
      : [];
    return list.length > 0
      ? `changed core settings (${list.map(cut).join(", ")})`
      : "changed core settings";
  }
  const name = cut(item.target_name ?? item.target_id ?? "unknown");
  const isUser = item.kind === "user";
  // "added integration Tuya", "added user Anna", "changed options of Tuya".
  const subject = isUser ? `user ${name}` : name;
  const withNoun = isUser ? `user ${name}` : `integration ${name}`;
  switch (item.action) {
    case "added":
      return `added ${withNoun}`;
    case "removed":
      return `removed ${withNoun}`;
    case "reconfigured":
      return `reconfigured ${subject}`;
    case "options_changed":
      return `changed options of ${subject}`;
    case "enabled":
    case "disabled":
      return `${item.action} ${subject}`;
    case "password_changed":
      return `changed the password of ${subject}`;
    case "updated":
      return `changed ${subject}`;
    default:
      return `changed ${subject}`;
  }
}

/** Friendly sentence for one posture check. */
export function postureText(item: PostureItem): string {
  const params: unknown = item.params;
  if (
    typeof params === "object" &&
    params !== null &&
    "check_failed" in params
  ) {
    return `This check could not run (${cut(String(params.check_failed))}).`;
  }
  switch (item.id) {
    case "admins_without_mfa": {
      const users = "users" in item.params ? item.params.users : [];
      if (users.length === 0) {
        return "Every administrator uses two-factor authentication.";
      }
      const names = users.map((user) => cut(user.name ?? "unnamed user"));
      return users.length === 1
        ? `1 administrator has no two-factor authentication: ${names.join(", ")}.`
        : `${users.length} administrators have no two-factor authentication: ${names.join(", ")}.`;
    }
    case "auto_ban_off": {
      const threshold =
        "threshold" in item.params ? item.params.threshold : null;
      if (threshold === null || threshold < 1) {
        return "Banning addresses after failed logins is turned off in Home Assistant.";
      }
      return item.level === "ok"
        ? `Home Assistant bans an address after ${threshold} failed logins.`
        : `Home Assistant will not ban addresses after failed logins (the limit is ${threshold}).`;
    }
    case "proxy_suspected": {
      if (item.level === "ok") {
        return "No sign of a reverse proxy hiding visitors' addresses.";
      }
      const ip = "ip" in item.params ? item.params.ip : null;
      return `Many different users seem to log in from one address${ip === null ? "" : ` (${cut(ip)})`}. That looks like a reverse proxy, and Home Assistant is not set to trust its forwarded address.`;
    }
    case "geoip_missing": {
      const p = item.params;
      if (item.level === "ok") {
        return "Country lookup is ready.";
      }
      const why =
        "last_error" in p && p.last_error !== null
          ? ` ${cut(p.last_error)}`
          : "";
      return `The country database is missing, so addresses show no country.${why}`;
    }
    case "collector_disabled": {
      if (item.level === "ok" || !("collector" in item.params)) {
        return "Electric Fence is watching everything it should.";
      }
      const { collector, reasons } = item.params;
      return `Electric Fence has switched off its ${cut(collector)} check${reasons.length > 0 ? `: ${reasons.map(cut).join(", ")}` : ""}.`;
    }
    case "storage_error": {
      if (item.level === "ok" || !("storage_errors" in item.params)) {
        return "History is being saved normally.";
      }
      const { storage_errors, last_error } = item.params;
      return `Electric Fence could not save ${storage_errors} ${storage_errors === 1 ? "record" : "records"}.${last_error === null ? "" : ` Last error: ${cut(last_error)}`}`;
    }
    default:
      // An id from a newer integration: say so rather than guess.
      return "This check is not known to this version of the panel.";
  }
}
