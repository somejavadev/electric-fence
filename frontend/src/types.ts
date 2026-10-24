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

export type ChangeKind = "config_entry" | "user" | "core_config";

export type ChangeAction =
  | "added"
  | "removed"
  | "updated"
  | "reconfigured"
  | "options_changed"
  | "enabled"
  | "disabled"
  | "password_changed";

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

export interface PostureItem {
  id: string;
  level: PostureLevel;
  /** Shape depends on `id`; see `shapes.py`. `threshold: null` means off. */
  params: Record<string, unknown>;
}

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

export interface GeoStatus {
  provider: "dbip_lite" | "maxmind" | "none";
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

export interface LoginFilter {
  result?: LoginResult;
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
    ha_login_notification: string;
    retention_days: number;
    geoip_provider: string;
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
