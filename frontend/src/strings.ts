import { monthsSince, truncate } from "./format.ts";
import type {
  BanItem,
  ChangeItem,
  IntegrationItem,
  PostureItem,
} from "./types.ts";
import { showIp } from "./mask.ts";

const OUTSIDE_ACTOR = "outside";

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
    integrations: "Custom integrations",
  },
  empty: {
    failed: "No failed logins in the last 24 hours",
    changes: "No settings changes in the last 7 days",
    success: "No successful logins recorded yet",
    posture: "No checks to show",
  },
  outside: "Outside Home Assistant",
  integrations: {
    checkNow: "Check now",
    started: "Started",
    recent: "A check is already running or ran in the last 5 minutes",
    scanError: "Could not start the check. Try again.",
    empty: "No custom integrations",
    unavailable: "Integration checks unavailable",
    open: "Show details",
    close: "Close",
    scannedAt: (when: string): string => `Last check ${when}`,
    sources: { marketplace: "Marketplace", unverified: "Unverified" } as Record<
      string,
      string
    >,
    version: (v: string): string => `Version ${v}`,
    files: {
      match: "Files match",
      changed: "Files changed",
      unchecked: "Couldn't check",
      baseline_recorded: "Baseline recorded",
      baseline_renewed: "Baseline renewed",
      off: "Checks off",
    } as Record<string, string>,
    health: {
      ok: "Up to date",
      archived: "Archived",
      removed: "Removed from the Marketplace",
      unknown: "Source unknown",
    } as Record<string, string>,
    unmaintained: (months: number | null): string =>
      months === null
        ? "Not updated recently"
        : `Not updated for ${months} ${months === 1 ? "month" : "months"}`,
    headings: {
      changed: "Changed files",
      missing: "Missing files",
      added: "Added files",
      unreadable: "Unreadable files",
      symlinks: "Symbolic links",
    },
    noFiles: "No file differences",
    repository: "Repository",
    ref: "Compared with",
    status: "Files",
    healthLabel: "Health",
    why: "Why it couldn't be checked",
    healthReason: "Reason",
    lastUpdated: "Last updated",
    truncated: "The lists are shortened.",
    /** Why a check could not run; others show as-is. */
    reasons: {
      no_reference: "No official copy to compare with",
      too_large: "The official copy is over the 25 MB limit",
      unreachable: "The official copy could not be downloaded",
      not_found: "The official copy was not found",
      bad_archive: "The official copy could not be read",
      rate_limited: "GitHub is limiting requests right now",
      contract_failed: "The Marketplace integration is not compatible",
      check_failed: "The check failed",
      install_in_progress: "An install or update is in progress",
    } as Record<string, string>,
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
  level: {
    ok: "OK",
    info: "Info",
    warning: "Warning",
    error: "Problem",
    critical: "Critical",
  },
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
    search: "Search",
    searchPlaceholder: "Search",
    filters: "Filters",
    clear: "Clear",
    on: "On",
    off: "Off",
    done: "Done",
    any: "Any",
    from: "From",
    to: "To",
    customRange: "Custom range",
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
      ip_ban: "IP bans",
      login_protection: "Login protection",
      app: "Apps",
      files: "Integration files",
      integration_health: "Integration health",
      forwarding: "Log forwarding",
    },
    anyAction: "Any action",
    action: "Action",
    actions: {
      added: "Added",
      removed: "Removed",
      updated: "Updated",
      reconfigured: "Reconfigured",
      options_changed: "Options changed",
      enabled: "Enabled",
      disabled: "Disabled",
      password_changed: "Password changed",
      restore_requested: "Restore requested",
      confirmed: "Confirmed",
      purged: "Purged",
      withdrawn: "Withdrawn",
      banned: "Banned",
      unbanned: "Unbanned",
      expired: "Expired",
      attack_started: "Attack started",
      attack_ended: "Attack ended",
      slowdown_ended: "Slowdown ended",
      countries_cleared: "Countries cleared",
      installed: "Installed",
      uninstalled: "Uninstalled",
      started: "Started",
      stopped: "Stopped",
      restarted: "Restarted",
      rebuilt: "Rebuilt",
      settings_changed: "Settings changed",
      repository_added: "Repository added",
      repository_removed: "Repository removed",
      files_changed: "Files changed",
      files_match: "Files match",
      baseline_recorded: "Baseline recorded",
      baseline_renewed: "Baseline renewed",
      archived: "Archived",
      removed_from_store: "Removed from store",
      unmaintained: "Unmaintained",
      recovered: "Recovered",
      forwarding_failed: "Forwarding failed",
      forwarding_recovered: "Forwarding recovered",
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
    apps: "Apps",
    fileLists: {
      changed: "Changed files",
      missing: "Missing files",
      added: "Added files",
      unreadable: "Unreadable files",
    },
    /** Detail lines of ban and login protection changes. */
    protection: {
      failures: "Failed attempts",
      expires: "Expires",
      never: "Never",
      banNumber: "Ban number",
      addresses: "Addresses",
      countries: "Countries",
      how: "How it ended",
      source: "Source",
      cleared: "Users cleared",
      reasons: {
        quiet: "Went quiet",
        ended_by_user: "Ended by a user",
      } as Record<string, string>,
    },
  },
  loadMore: "Load more",
  download: {
    menu: "More actions",
    csv: "Download data (CSV)",
    json: "Download data (JSON)",
    preparing: (n: number): string => `Preparing… ${n} rows`,
    failed: "Could not prepare the download. Nothing was saved.",
    truncated: (n: number): string =>
      `The download holds the newest ${n} rows only.`,
  },
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
  newCountry: "New country",
  country: "Country",
  /** For a value on its own line, where "local" would start lowercase. */
  countryLocal: "Local network",
  countryUnknown: "Unknown country",
  confirm: {
    cancel: "Cancel",
    close: "Close",
  },
  bans: {
    heading: "Bans",
    state: "Show",
    states: { active: "Active", history: "History" },
    source: "Source",
    anySource: "All",
    sources: { electric_fence: "Electric Fence", other: "Other" },
    empty: "No active bans",
    emptyHistory: "No ended bans yet",
    emptyFiltered: "No bans match these filters",
    unavailable: {
      ip_ban_disabled:
        "IP banning is off in Home Assistant's HTTP settings (ip_ban_enabled).",
      other:
        "IP banning is unavailable right now. Electric Fence keeps recording logins.",
    },
    never: "Never",
    ended: "Ended",
    expiresMinutes: (n: number): string => `Expires in ${n} min`,
    expiresHours: (n: number): string => `Expires in ${n} h`,
    expiresDays: (n: number): string => `Expires in ${n} d`,
    open: "Show details",
    hostname: "Host name",
    failures: "Failed attempts",
    banNumber: "Ban number",
    bannedAt: "Banned at",
    expires: "Expires",
    endedAt: "Ended",
    how: "How it ended",
    endReasons: {
      expired: "Expired",
      unbanned: "Unbanned",
      removed: "Removed outside Electric Fence",
    },
    /** `{}` marks where a value goes; see `fill` in text.ts. */
    unbannedBy: "Unbanned by {}",
    unknown: "Unknown",
    unban: "Unban",
    unbanTitle: "Unban {}?",
    unbanText: "It can log in again straight away.",
    unbanError: "Could not unban this address. Try again.",
  },
  attacks: {
    heading: "Under attack",
    activeBans: "Active bans",
    bans24h: (n: number): string => `${n} in the last 24 hours`,
    unavailable: "IP banning is unavailable",
    failures: (n: number): string =>
      n === 1 ? "1 failed attempt" : `${n} failed attempts`,
    addresses: (n: number): string =>
      n === 1 ? "1 address" : `${n} addresses`,
    endSlowdown: "End slow-down",
    endTitle: "End the slow-down for {}?",
    endText: "Logins with this username are no longer delayed.",
    endError: "Could not end the slow-down. Try again.",
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
    apps: "Apps",
    appAuditing: "App auditing",
    appsNotAvailable: "Not available on this install type",
    appsDisabled:
      "App auditing is turned off because Home Assistant changed (see Repairs)",
    forwarding: "Log forwarding",
    forwardingState: "Forwarding",
    forwardingStates: { connected: "Connected", failing: "Failing" } as Record<
      string,
      string
    >,
    integrationChecks: "Integration checks",
    fileChecks: "File checks",
    repairs: "Repairs",
    open: "Open settings",
    openHint:
      "Options are changed in Home Assistant's integration settings, not here.",
    bans: "Bans",
    autoBan: "Ban automatically",
    banAfter: "Ban after",
    banRule: (failures: number, minutes: number): string =>
      `${failures} failed ${failures === 1 ? "attempt" : "attempts"} within ${minutes} min`,
    banLength: "Ban length",
    hours: (n: number): string => `${n} h`,
    permanent: "Permanent",
    repeatOffenders: "Lengthen bans for repeat offenders",
    ipv6Prefix: "IPv6 counted per",
    exemptLocal: "Exempt local addresses",
    usernameProtection: "Username protection",
    protectUsernames: "Protect usernames",
    slowDownAfter: "Slow down after",
    attackRule: (failures: number, ips: number, minutes: number): string =>
      `${failures} failed ${failures === 1 ? "attempt" : "attempts"} from at least ${ips} ${ips === 1 ? "address" : "addresses"} within ${minutes} min`,
    notifications: "Notifications",
    notifyFailedLogin: "Failed login",
    notifyIpBanned: "IP address banned",
    notifyUsernameAttack: "Username under attack",
    notifyNewCountry: "Login from a new country",
    notifyBanEnded: "Ban ended",
    notifyTargets: "Phone notifications",
    knownCountries: "Known countries",
    knownCountriesHint:
      "Electric Fence remembers the countries each user has logged in from, to spot a login from a new one.",
    clearCountries: "Clear known countries",
    clearTitle: "Clear known countries?",
    clearText:
      "Electric Fence forgets which countries every user has logged in from.",
    clearAction: "Clear",
    clearError: "Could not clear known countries. Try again.",
    cleared: "Known countries cleared",
  },
} as const;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

/**
 * When a ban ends, read at render time: "Expires in 3 h", "Never" or
 * "Ended". A ban past its end but not yet lifted reads "Expires in 1 min".
 */
export function banExpiry(
  ban: Pick<BanItem, "expires_at" | "ended_at">,
  now: number = Date.now(),
): string {
  if (ban.ended_at !== null) {
    return strings.bans.ended;
  }
  if (ban.expires_at === null) {
    return strings.bans.never;
  }
  const left = Date.parse(ban.expires_at) - now;
  if (Number.isNaN(left)) {
    return strings.bans.unknown;
  }
  const b = strings.bans;
  const minutes = Math.max(1, Math.round(left / MINUTE));
  if (minutes < 60) {
    return b.expiresMinutes(minutes);
  }
  if (left < 48 * HOUR) {
    return b.expiresHours(Math.round(left / HOUR));
  }
  return b.expiresDays(Math.round(left / (24 * HOUR)));
}

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

/** Sentence for a ban or login protection change; undefined for others. */
function protectionSentence(item: ChangeItem): string | undefined {
  if (item.kind === "ip_ban") {
    const ip = cut(
      item.target_id === null ? strings.unknownIp : showIp(item.target_id),
    );
    switch (item.action) {
      case "banned":
        return `banned ${ip}`;
      case "unbanned":
        return `unbanned ${ip}`;
      case "expired":
        return `ban of ${ip} expired`;
      case "removed":
        return `ban of ${ip} was removed from ip_bans.yaml`;
      default:
        return `changed the ban of ${ip}`;
    }
  }
  if (item.kind === "login_protection") {
    const name = cut(item.target_name ?? strings.unknownUser);
    switch (item.action) {
      case "attack_started":
        return `username ${name} came under attack`;
      case "attack_ended":
        return `attack on ${name} ended`;
      case "slowdown_ended":
        return `ended the slow-down for ${name}`;
      case "countries_cleared":
        return "cleared known login countries";
      default:
        return "changed login protection";
    }
  }
  return undefined;
}

/** A System change: no user, and not made outside Home Assistant. */
export function isSystemChange(
  item: Pick<ChangeItem, "actor_user_id" | "actor_name">,
): boolean {
  return item.actor_user_id === null && item.actor_name !== OUTSIDE_ACTOR;
}

/** Who made a change: a user, System, or a change made outside Home Assistant. */
export function actorLabel(
  item: Pick<ChangeItem, "actor_user_id" | "actor_name">,
): string {
  if (item.actor_user_id === null && item.actor_name === OUTSIDE_ACTOR) {
    return strings.outside;
  }
  return item.actor_name ?? strings.system;
}

const text = (value: unknown): string | undefined =>
  typeof value === "string" && value !== "" ? value : undefined;

/** "version 6.5 → 6.6" when both are known. */
function versions(details: Record<string, unknown>): string {
  const before = text(details["version_before"]);
  const after = text(details["version_after"]);
  return before !== undefined && after !== undefined
    ? ` (version ${cut(before)} → ${cut(after)})`
    : "";
}

function appSentence(item: ChangeItem): string {
  const d = item.details;
  const apps = d["apps"];
  const fallback = Array.isArray(apps) ? `${apps.length} apps` : "unknown app";
  const name = cut(item.target_name ?? item.target_id ?? fallback);
  const background = d["background"] === true;
  switch (item.action) {
    case "installed":
      return background
        ? `started installing ${name}`
        : `installed app ${name}`;
    case "uninstalled":
      return `uninstalled app ${name}`;
    case "updated":
      return background
        ? `started updating ${name}`
        : `updated ${name}${versions(d)}`;
    case "started":
    case "stopped":
    case "restarted":
    case "rebuilt":
      return `${item.action} ${name}`;
    case "options_changed":
      return d["outside"] === true
        ? `changed options of ${name} (values not recorded)`
        : `changed options of ${name}`;
    case "settings_changed":
      return `changed settings of ${name}`;
    case "repository_added":
      return item.target_id === null
        ? "added an app repository"
        : `added app repository ${cut(item.target_id)}`;
    case "repository_removed":
      return item.target_id === null
        ? "removed an app repository"
        : `removed app repository ${cut(item.target_id)}`;
    default:
      return `changed ${name}`;
  }
}

function listLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function filesSentence(item: ChangeItem): string {
  const name = cut(item.target_name ?? item.target_id ?? "unknown");
  switch (item.action) {
    case "files_changed": {
      const d = item.details;
      const n =
        listLength(d["changed"]) +
        listLength(d["missing"]) +
        listLength(d["added"]) +
        listLength(d["unreadable"]);
      return n === 0
        ? `files of ${name} differ from the official copy`
        : `files of ${name} differ from the official copy (${n} ${n === 1 ? "file" : "files"})`;
    }
    case "files_match":
      return `files of ${name} match the official copy`;
    case "baseline_recorded":
      return `recorded the files of ${name} as the baseline`;
    case "baseline_renewed":
      return `renewed the file baseline of ${name} after an update`;
    default:
      return `changed the file check of ${name}`;
  }
}

function healthSentence(item: ChangeItem): string {
  const name = cut(item.target_name ?? item.target_id ?? "unknown");
  switch (item.action) {
    case "removed_from_store":
      return `${name} was removed from the Marketplace`;
    case "archived":
      return `the repository of ${name} was archived`;
    case "unmaintained": {
      const months = item.details["months"];
      return typeof months === "number"
        ? `${name} has not been updated for ${months} ${months === 1 ? "month" : "months"}`
        : `${name} has not been updated for a while`;
    }
    case "recovered":
      return `${name} is healthy again`;
    default:
      return `changed the health of ${name}`;
  }
}

function forwardingSentence(item: ChangeItem): string {
  if (item.action === "forwarding_failed") {
    const error = text(item.details["error"]);
    return error === undefined
      ? "forwarding to the log server failed"
      : `forwarding to the log server failed (${cut(error)})`;
  }
  if (item.action === "forwarding_recovered") {
    return "forwarding to the log server recovered";
  }
  return "changed log forwarding";
}

/** Sentence for an app, file check, health or forwarding change. */
function auditSentence(item: ChangeItem): string | undefined {
  switch (item.kind) {
    case "app":
      return appSentence(item);
    case "files":
      return filesSentence(item);
    case "integration_health":
      return healthSentence(item);
    case "forwarding":
      return forwardingSentence(item);
    default:
      return undefined;
  }
}

/** Sentence for a settings change, such as "added integration Tuya". */
export function changeSentence(item: ChangeItem): string {
  const protection = protectionSentence(item);
  if (protection !== undefined) {
    return protection;
  }
  const audit = auditSentence(item);
  if (audit !== undefined) {
    return audit;
  }
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

const plural = (n: number): string =>
  `${n} custom ${n === 1 ? "integration" : "integrations"}`;

const domains = (list: string[]): string => list.map(cut).join(", ");

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
    case "ip_ban_disabled":
      return item.level === "ok"
        ? "Home Assistant's IP ban is on."
        : "Home Assistant's IP ban is off, so no address can be banned. Turn on ip_ban_enabled in its HTTP settings.";
    case "auto_ban_off": {
      if ("electric_fence" in item.params && item.params.electric_fence) {
        return "Electric Fence bans an address after repeated failed logins.";
      }
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
      return `Many different users seem to log in from one address${ip === null ? "" : ` (${cut(showIp(ip))})`}. That looks like a reverse proxy, and Home Assistant is not set to trust its forwarded address.`;
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
    case "forwarding_failing": {
      const p = item.params;
      if (item.level === "ok" || !("queued" in p)) {
        return "Forwarding logs to your log server is working.";
      }
      const dropped =
        p.dropped > 0
          ? ` and ${p.dropped} ${p.dropped === 1 ? "was" : "were"} dropped`
          : "";
      const why = p.last_error === null ? "" : ` (${cut(p.last_error)})`;
      return `Forwarding logs to your log server is failing${why}. ${p.queued} ${p.queued === 1 ? "event is" : "events are"} waiting${dropped}.`;
    }
    case "integration_files_changed": {
      const list =
        "integrations" in item.params ? item.params.integrations : [];
      return list.length === 0
        ? "Custom integration files match their official copies."
        : `${plural(list.length)} differ${list.length === 1 ? "s" : ""} from their official copy: ${domains(list)}.`;
    }
    case "integration_health": {
      const { removed, archived, unmaintained } =
        "removed" in item.params
          ? item.params
          : { removed: [], archived: [], unmaintained: [] };
      const parts: string[] = [];
      if (removed.length > 0) {
        parts.push(
          `${plural(removed.length)} ${removed.length === 1 ? "was" : "were"} removed from the Marketplace: ${domains(removed)}.`,
        );
      }
      if (archived.length > 0) {
        parts.push(
          `${archived.length} ${archived.length === 1 ? "is" : "are"} archived: ${domains(archived)}.`,
        );
      }
      if (unmaintained.length > 0) {
        parts.push(
          `${unmaintained.length} ${unmaintained.length === 1 ? "has" : "have"} not been updated for a while: ${domains(unmaintained)}.`,
        );
      }
      return parts.length === 0
        ? "Custom integrations are maintained."
        : parts.join(" ");
    }
    case "integration_checks_unchecked": {
      const list =
        "integrations" in item.params ? item.params.integrations : [];
      return list.length === 0
        ? "Every custom integration could be checked."
        : `${plural(list.length)} could not be checked: ${domains(list)}.`;
    }
    default:
      // An id from a newer integration: say so rather than guess.
      return "This check is not known to this version of the panel.";
  }
}

/** The health of an integration in a few words. */
export function integrationHealth(item: IntegrationItem): string {
  const t = strings.integrations;
  const h = item.health;
  if (h.status === "unmaintained") {
    return t.unmaintained(monthsSince(h.last_updated));
  }
  return Object.hasOwn(t.health, h.status)
    ? (t.health[h.status] ?? "")
    : h.status;
}

/** The file check of an integration in a few words. */
export function integrationFiles(item: IntegrationItem): string {
  const t = strings.integrations;
  return item.files === null
    ? (t.files["off"] ?? "")
    : (t.files[item.files.status] ?? item.files.status);
}
