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
  | "blocked_local_only";

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
  | "recorder";

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

export type PostureLevel = "ok" | "warning" | "error";

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
  /** `threshold: null` means ban handling is off entirely. */
  | PostureBase<"auto_ban_off", { threshold: number | null }>
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
    >;

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

export interface Overview {
  failed_24h: { count: number; items: LoginItem[] };
  changes_7d: { count: number; items: ChangeItem[] };
  recent_success: { items: LoginItem[] };
  posture: PostureItem[];
}

/** A `result` filter: one result, or failed and blocked together. */
export type ResultFilter = LoginResult | "failed_or_blocked";

export interface LoginFilter {
  result?: ResultFilter;
  user_id?: string;
  username?: string;
  ip?: string;
  country?: string;
  since?: string;
  until?: string;
  before_id?: number;
  limit?: number;
}

export interface ChangeFilter {
  kind?: ChangeKind;
  actor_user_id?: string;
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
    geoip_provider: GeoProvider;
    maxmind_account_id: string | null;
    reverse_dns: boolean;
    maxmind_license_key_set: boolean;
  };
  geo: GeoStatus | null;
  collectors: { name: string; enabled: boolean; reason: string[] }[];
  worker: { queued: number; dropped: number; storage_errors: number };
}

/** Pushed by `electric_fence/subscribe`. Pushed items have `id: null`. */
export type SubscribeEvent =
  | { type: "login"; item: LoginItem }
  | { type: "change"; item: ChangeItem }
  | { type: "unloaded" };

/** WebSocket error codes the commands answer with. */
export type ApiErrorCode =
  "unauthorized" | "not_loaded" | "invalid_format" | (string & {});
