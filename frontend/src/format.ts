import type { HomeAssistant, LoginReason } from "./types.ts";

const REASON_LABELS: Record<LoginReason, string> = {
  ok: "ok",
  no_such_user: "no such user",
  wrong_password: "wrong password",
  invalid_credentials: "invalid credentials",
  wrong_2fa_code: "wrong two-factor code",
  invalid_token: "invalid token",
  blocked_inactive_user: "blocked: inactive user",
  blocked_local_only: "blocked: local only",
};

/** Format an ISO timestamp in the user's language and the server time zone. */
export function formatTime(iso: string, hass: HomeAssistant): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat(hass.locale.language, {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: hass.config.time_zone,
  }).format(date);
}

/** Regional-indicator flag emoji for an ISO alpha-2 code, else "". */
export function countryFlag(code: string | null): string {
  if (code === null || !/^[A-Za-z]{2}$/.test(code)) {
    return "";
  }
  return String.fromCodePoint(
    ...[...code.toUpperCase()].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65),
  );
}

/** Cut text to `max` characters plus an ellipsis; keep the full text. */
export function truncate(
  text: string,
  max: number,
): { short: string; full: string } {
  const chars = [...text];
  if (chars.length <= max) {
    return { short: text, full: text };
  }
  return { short: `${chars.slice(0, max).join("")}…`, full: text };
}

export function reasonLabel(reason: LoginReason | string): string {
  return (REASON_LABELS as Record<string, string>)[reason] ?? reason;
}

export type TimeRange = "24h" | "7d" | "30d" | "90d" | "all";

export const TIME_RANGES: readonly TimeRange[] = [
  "24h",
  "7d",
  "30d",
  "90d",
  "all",
];

const RANGE_MS: Record<Exclude<TimeRange, "all">, number> = {
  "24h": 24 * 3600_000,
  "7d": 7 * 24 * 3600_000,
  "30d": 30 * 24 * 3600_000,
  "90d": 90 * 24 * 3600_000,
};

/** ISO UTC start of a range; `undefined` for "all" (no lower bound). */
export function sinceFor(
  range: TimeRange,
  now: number = Date.now(),
): string | undefined {
  return range === "all"
    ? undefined
    : new Date(now - RANGE_MS[range]).toISOString();
}
