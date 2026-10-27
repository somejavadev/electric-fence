/** Types mirroring the JSON contract in `shapes.py` of the integration. */

export type UnsubscribeFunc = () => void;

/** The small part of Home Assistant's `hass` object the panel relies on. */
export interface HomeAssistant {
  connection: {
    sendMessagePromise<T>(message: { type: string } & object): Promise<T>;
    subscribeMessage<T>(
      callback: (message: T) => void,
      subscribeMessage: { type: string } & object,
    ): Promise<UnsubscribeFunc>;
  };
  locale: { language: string };
  config: { time_zone: string };
  language: string;
}

export interface Route {
  prefix: string;
  path: string;
}

export type LoginResult = "success" | "failed" | "blocked";

export type LoginReason =
  | "ok"
  | "no_such_user"
  | "wrong_password"
  | "invalid_credentials"
  | "wrong_2fa_code"
  | "invalid_token"
  | "blocked_inactive_user"
  | "blocked_local_only"
  | "throttled";

export type Source = "local" | "remote" | "cloud";

/** A stored login attempt. `id` is null in subscription events. */
export interface LoginItem {
  id: number | null;
  ts: string;
  result: LoginResult;
  reason: LoginReason;
  /** Raw attacker-controlled text: render as text only. */
  username_typed: string | null;
  user_exists: "yes" | "no" | "unknown";
  user_id: string | null;
  /** The user's name from the auth store, looked up by `user_id`. */
  user_name: string | null;
  provider: string | null;
  ip: string | null;
  /** Uppercase ISO 3166-1 alpha-2, or null. */
  country_code: string | null;
  hostname: string | null;
  source: Source | null;
  client_id: string | null;
  client_name: string | null;
  user_agent: string | null;
  url: string | null;
  mfa_module: string | null;
  confirmed: boolean;
  count: number;
  /** A confirmed success from a country this user never logged in from. */
  new_country: boolean;
}

export type KnownChangeKind =
  | "config_entry"
  | "user"
  | "core_config"
  | "backup"
  | "http"
  | "network"
  | "analytics"
  | "logger"
  | "recorder"
  | "ip_ban"
  | "login_protection"
  | "app"
  | "files"
  | "integration_health"
  | "forwarding";

/** A kind from a newer or older database may be any string. */
export type ChangeKind = KnownChangeKind | (string & Record<never, never>);

export type ChangeAction =
  | "added"
  | "removed"
  | "updated"
  | "reconfigured"
  | "options_changed"
  | "enabled"
  | "disabled"
  | "password_changed"
  | "restore_requested"
  | "confirmed"
  | "purged"
  | "withdrawn"
  | "banned"
  | "unbanned"
  | "expired"
  | "attack_started"
  | "attack_ended"
  | "slowdown_ended"
  | "countries_cleared"
  | "installed"
  | "uninstalled"
  | "started"
  | "stopped"
  | "restarted"
  | "rebuilt"
  | "settings_changed"
  | "repository_added"
  | "repository_removed"
  | "files_changed"
  | "files_match"
  | "baseline_recorded"
  | "baseline_renewed"
  | "baseline_accepted"
  | "archived"
  | "removed_from_store"
  | "unmaintained"
  | "recovered"
  | "forwarding_failed"
  | "forwarding_recovered"
  | (string & Record<never, never>);

/** A stored settings change. A null `actor_user_id` means System. */
export interface ChangeItem {
  id: number | null;
  ts: string;
  kind: ChangeKind;
  action: ChangeAction;
  target_id: string | null;
  target_name: string | null;
  details: Record<string, unknown>;
  actor_user_id: string | null;
  actor_name: string | null;
  actor_ip: string | null;
}

export type PostureLevel = "ok" | "info" | "warning" | "error" | "critical";

/** A check that itself failed: `level` is "error". */
export interface CheckFailed {
  check_failed: string;
}

interface PostureBase<Id extends string, Params> {
  id: Id;
  level: PostureLevel;
  params: Params | CheckFailed;
}

export type PostureItem =
  | PostureBase<
      "admins_without_mfa",
      { users: { id: string; name: string | null }[] }
    >
  /**
   * `threshold` is Core's own login limit (null: off); `electric_fence` is
   * whether Electric Fence's auto-ban is on and can ban.
   */
  | PostureBase<
      "auto_ban_off",
      { threshold: number | null; electric_fence: boolean }
    >
  /** Core's IP ban is off, so nothing can be banned. */
  | PostureBase<"ip_ban_disabled", Record<string, never>>
  | PostureBase<
      "proxy_suspected",
      {
        ip: string | null;
        users: number;
        use_x_forwarded_for: boolean | null;
      }
    >
  | PostureBase<
      "geoip_missing",
      { provider: string; last_error: string | null }
    >
  /** `{}` when ok; one error item per disabled collector otherwise. */
  | PostureBase<
      "collector_disabled",
      Record<string, never> | { collector: string; reasons: string[] }
    >
  | PostureBase<
      "storage_error",
      {
        storage_errors: number;
        last_error: string | null;
        last_error_at: string | null;
      }
    >
  | PostureBase<
      "forwarding_failing",
      {
        last_error: string | null;
        failing_since: string | null;
        queued: number;
        dropped: number;
      }
    >
  /** Domains whose files differ from the official copy. */
  | PostureBase<"integration_files_changed", { integrations: string[] }>
  | PostureBase<
      "integration_health",
      { removed: string[]; archived: string[]; unmaintained: string[] }
    >
  /** Domains that could not be checked. */
  | PostureBase<"integration_checks_unchecked", { integrations: string[] }>;

export interface SessionItem {
  id: string;
  client_name: string | null;
  client_id: string | null;
  token_type: string;
  created_at: string;
  last_used_at: string | null;
  last_used_ip: string | null;
}

export interface UserItem {
  id: string;
  name: string | null;
  username: string | null;
  is_admin: boolean;
  is_owner: boolean;
  is_active: boolean;
  local_only: boolean;
  system_generated: boolean;
  mfa_modules: string[];
  last_login: LoginItem | null;
  sessions: SessionItem[];
}

export type GeoProvider = "dbip_lite" | "maxmind" | "none";

export interface GeoStatus {
  provider: GeoProvider;
  db_date: string | null;
  last_error: string | null;
  attribution: string | null;
}

export type BanSource = "electric_fence" | "other";

export type BanEndReason = "expired" | "unbanned" | "removed";

/** An IP ban, active or ended. "other" bans have no counts. */
export interface BanItem {
  ip: string;
  source: BanSource;
  banned_at: string;
  /** Null: permanent. */
  expires_at: string | null;
  failures: number | null;
  ban_number: number | null;
  country_code: string | null;
  /** Raw text from reverse DNS: render as text only. */
  hostname: string | null;
  ended_at: string | null;
  end_reason: BanEndReason | null;
  /** Who unbanned it; null when not by a user. */
  ended_by_name: string | null;
}

export type BanState = "active" | "history";

/** `reason` says why bans cannot be applied right now. */
export interface BansPage extends Page<BanItem> {
  available: boolean;
  reason:
    | "not_loaded"
    | "contracts"
    | "ip_ban_disabled"
    | "backend_error"
    | (string & {})
    | null;
}

/** A username under attack. */
export interface AttackItem {
  /** Raw attacker-controlled text: render as text only. */
  username: string;
  started_at: string;
  ended_at: string | null;
  failures: number;
  ip_count: number;
  countries: string[];
  end_reason: "quiet" | "ended_by_user" | null;
  active: boolean;
}

export interface Protection {
  available: boolean;
  active_bans: number;
  bans_24h: number;
  /** Active attacks only. */
  attacks: AttackItem[];
}

export interface Overview {
  failed_24h: { count: number; items: LoginItem[] };
  changes_7d: { count: number; items: ChangeItem[] };
  recent_success: { items: LoginItem[] };
  posture: PostureItem[];
  protection: Protection;
}

/** A `result` filter: one result, or failed and blocked together. */
export type ResultFilter = LoginResult | "failed_or_blocked";

export interface LoginFilter {
  result?: ResultFilter;
  user_id?: string;
  username?: string;
  ip?: string;
  country?: string;
  search?: string;
  since?: string;
  until?: string;
  before_id?: number;
  limit?: number;
}

export interface ChangeFilter {
  kind?: ChangeKind;
  actor_user_id?: string;
  action?: ChangeAction;
  ip?: string;
  search?: string;
  include_system?: boolean;
  since?: string;
  until?: string;
  before_id?: number;
  limit?: number;
}

export interface Page<T> {
  items: T[];
  next_before_id: number | null;
}

export interface Status {
  version: string | null;
  settings: {
    notify_failed_login: boolean;
    ha_login_notification: "replace" | "keep";
    retention_days: number;
    max_db_mb: number;
    geoip_provider: GeoProvider;
    maxmind_account_id: string | null;
    reverse_dns: boolean;
    record_values: boolean;
    auto_ban: boolean;
    ban_threshold: number;
    ban_window_minutes: number;
    /** 0 means bans never expire. */
    ban_hours: number;
    ban_repeat_offenders: boolean;
    username_protection: boolean;
    attack_threshold: number;
    attack_min_ips: number;
    attack_window_minutes: number;
    notify_ip_banned: boolean;
    notify_username_attack: boolean;
    notify_new_country: boolean;
    notify_ban_ended: boolean;
    /** The panel masks addresses and host names; the data stays complete. */
    mask_ips: boolean;
    /** Notify entity IDs. */
    notify_targets: string[];
    maxmind_license_key_set: boolean;
    verify_files: boolean;
    integration_repairs: boolean;
    forward_enabled: boolean;
    forward_ca_pem_set: boolean;
  };
  geo: GeoStatus | null;
  collectors: { name: string; enabled: boolean; reason: string[] }[];
  worker: { queued: number; dropped: number; storage_errors: number };
  /** Null while forwarding is off. */
  forwarding: ForwardingStatus | null;
  /** `disabled`: Supervisor install, turned off by a failed contract. */
  apps: "available" | "disabled" | "not_available";
}

export interface ForwardingStatus {
  state: "connected" | "failing";
  queued: number;
  dropped: number;
  last_error: string | null;
}

export type FilesStatus =
  "match" | "changed" | "unchecked" | "baseline_recorded" | "baseline_renewed";

export type HealthStatus =
  "ok" | "unmaintained" | "archived" | "removed" | "unknown";

export interface FileReport {
  changed: string[];
  missing: string[];
  added: string[];
  unreadable: string[];
  symlinks: string[];
}

/** Text fields (name, version, paths, repository) are attacker-controlled. */
export interface IntegrationItem {
  domain: string;
  name: string;
  source: "marketplace" | "unverified" | (string & {});
  version: string | null;
  repository: string | null;
  /** Null while file checks are off. */
  files: {
    status: FilesStatus;
    reason: string | null;
    ref: string | null;
    version_before: string | null;
    report: FileReport | null;
  } | null;
  health: {
    status: HealthStatus;
    level: "ok" | "notice" | "warning" | "critical";
    last_updated: string | null;
    reason: string | null;
  };
  truncated: boolean;
}

export interface IntegrationsPage {
  items: IntegrationItem[];
  scanned_at: string | null;
  running: boolean;
  available: boolean;
}

/** Pushed by `electric_fence/subscribe`. Pushed items have `id: null`. */
export type SubscribeEvent =
  | { type: "login"; item: LoginItem }
  | { type: "change"; item: ChangeItem }
  | { type: "unloaded" };

/** WebSocket error codes the commands answer with. */
export type ApiErrorCode =
  "unauthorized" | "not_loaded" | "invalid_format" | (string & {});
