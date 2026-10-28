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
  throttled: "Too many attempts",
  banned: "Blocked: address banned",
};

/** Longest user-supplied text shown before it is cut. */
export const MAX_TEXT = 64;

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

// C1 controls and the invisible and direction characters: `\u202enimda`
// reads as "admin", `admin\u200b` looks like "admin". The server shows them
// this way too; older rows may still hold them.
const HIDDEN = /[\u0080-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]/g;

/** Show hidden characters as their code point, e.g. `⟨U+202E⟩`. */
export const visible = (text: string): string =>
  text.replace(
    HIDDEN,
    (c) =>
      `⟨U+${(c.codePointAt(0) ?? 0).toString(16).toUpperCase().padStart(4, "0")}⟩`,
  );

/**
 * Cut text to `max` characters plus an ellipsis; keep the full text. Hidden
 * characters are shown in both, and count as one character towards `max`.
 */
export function truncate(
  value: string,
  max: number,
): { short: string; full: string } {
  const chars = [...value];
  const full = visible(value);
  if (chars.length <= max) {
    return { short: full, full };
  }
  return { short: `${visible(chars.slice(0, max).join(""))}…`, full };
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

/** A time range, or two chosen days (`from` and `to`) when "custom". */
export type RangeChoice = TimeRange | "custom";

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A `YYYY-MM-DD` day as a local date; `undefined` when it isn't one. */
function localDay(day: string, end: boolean): Date | undefined {
  const match = DAY.exec(day);
  if (match === null) {
    return undefined;
  }
  const [year, month, date] = match.slice(1).map(Number) as [
    number,
    number,
    number,
  ];
  const value = end
    ? new Date(year, month - 1, date, 23, 59, 59, 999)
    : new Date(year, month - 1, date);
  return value.getDate() === date ? value : undefined;
}

export interface Bounds {
  since?: string;
  until?: string;
}

/**
 * The `since` and `until` of a range as ISO UTC. Custom days are read in the
 * browser's time zone: `since` is the start of `from`, `until` the end of `to`.
 */
export function rangeBounds(
  range: RangeChoice,
  from: string,
  to: string,
  now: number = Date.now(),
): Bounds {
  if (range !== "custom") {
    const since = sinceFor(range, now);
    return since === undefined ? {} : { since };
  }
  // A start after the end means the days were picked the wrong way round.
  if (from !== "" && to !== "" && from > to) {
    [from, to] = [to, from];
  }
  const out: Bounds = {};
  const start = localDay(from, false);
  const end = localDay(to, true);
  if (start !== undefined) {
    out.since = start.toISOString();
  }
  if (end !== undefined) {
    out.until = end.toISOString();
  }
  return out;
}

/** Whether an ISO timestamp lies inside the bounds (ends included). */
export function withinBounds(ts: string, bounds: Bounds): boolean {
  const time = Date.parse(ts);
  return (
    (bounds.since === undefined || time >= Date.parse(bounds.since)) &&
    (bounds.until === undefined || time <= Date.parse(bounds.until))
  );
}

/** SQLite LIKE folds ASCII letters only; so does this. */
export const asciiLower = (text: string): string =>
  text.replace(/[A-Z]/g, (c) => c.toLowerCase());

/** Whether any field contains the text, ignoring ASCII case. */
export function containsText(
  fields: readonly (string | null | undefined)[],
  needle: string,
): boolean {
  if (needle === "") {
    return true;
  }
  const lower = asciiLower(needle);
  return fields.some((f) => asciiLower(f ?? "").includes(lower));
}

/** Whether the text is a `YYYY-MM-DD` day (or empty). */
export const isDayOrEmpty = (text: string): boolean =>
  text === "" || localDay(text, false) !== undefined;

const MONTH_MS = 30.44 * 24 * 60 * 60 * 1000;

/** Whole months since `iso`; null when unknown or in the future. */
export function monthsSince(
  iso: string | null,
  now: number = Date.now(),
): number | null {
  if (iso === null) {
    return null;
  }
  const then = Date.parse(iso);
  if (Number.isNaN(then) || then > now) {
    return null;
  }
  return Math.floor((now - then) / MONTH_MS);
}
