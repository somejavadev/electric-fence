import type { HomeAssistant, LoginReason } from "./types.ts";

const REASON_LABELS: Record<LoginReason, string> = {
  ok: "OK",
  no_such_user: "No such user",
  wrong_password: "Wrong password",
  invalid_credentials: "Invalid credentials",
  wrong_2fa_code: "Wrong two-factor code",
  invalid_token: "Invalid token",
  blocked_inactive_user: "Blocked: inactive user",
  blocked_local_only: "Blocked: local only",
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

/**
 * Format a `YYYY-MM-DD` date as a calendar date only. It is read and shown in
 * UTC, so no time zone moves it to another day. Other input is returned as is.
 */
export function formatDate(day: string, hass: HomeAssistant): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if (match === null) {
    return day;
  }
  const [year, month, date] = match.slice(1).map(Number) as [
    number,
    number,
    number,
  ];
  const value = new Date(Date.UTC(year, month - 1, date));
  if (
    value.getUTCFullYear() !== year ||
    value.getUTCMonth() !== month - 1 ||
    value.getUTCDate() !== date
  ) {
    return day;
  }
  return new Intl.DateTimeFormat(hass.locale.language, {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(value);
}

/** Regional-indicator flag emoji for an ISO alpha-2 code, else "". */
export function countryFlag(code: string | null): string {
  if (code === null || !/^[A-Za-z]{2}$/.test(code)) {
    return "";
  }
  return String.fromCodePoint(
    ...[...code.toUpperCase()].map(
      (c) => 0x1f1e6 + (c.codePointAt(0) ?? 65) - 65,
    ),
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

export function reasonLabel(reason: string): string {
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
  "24h": 24 * 3_600_000,
  "7d": 7 * 24 * 3_600_000,
  "30d": 30 * 24 * 3_600_000,
  "90d": 90 * 24 * 3_600_000,
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
