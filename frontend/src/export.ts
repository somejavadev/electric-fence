import type { ChangeItem, LoginItem, Page } from "./types.ts";

/** Most rows one export holds. */
export const EXPORT_CAP = 100_000;

/** The server's largest page. */
export const EXPORT_PAGE = 200;

const BOM = String.fromCharCode(0xfeff);
const FORMULA_START = /^[=+\-@\t\r]/;
const NEEDS_QUOTES = /[,"\n\r]|^ | $/;

/**
 * One CSV field. A leading `= + - @ \t \r` gets a `'` in front so a
 * spreadsheet never runs it as a formula (usernames come from attackers).
 */
export function csvSafe(value: unknown): string {
  let text: string;
  if (value === null || value === undefined) {
    return "";
  } else if (typeof value === "object") {
    text = JSON.stringify(value);
  } else {
    text = String(value);
  }
  if (FORMULA_START.test(text)) {
    text = `'${text}`;
  }
  return NEEDS_QUOTES.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

/** CSV with CRLF line ends and a UTF-8 BOM, so Excel reads non-Latin text. */
export function toCsv(header: string[], rows: unknown[][]): string {
  const lines = [header, ...rows].map((row) => row.map(csvSafe).join(","));
  return `${BOM}${lines.join("\r\n")}\r\n`;
}

/** Every page of a filter, newest first, up to `cap` rows. */
export async function collectAll<T>(
  fetchPage: (
    beforeId: number | null,
  ) => Promise<{ items: T[]; next_before_id: number | null }>,
  cap: number = EXPORT_CAP,
  onProgress?: (n: number) => void,
): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = [];
  let before: number | null = null;
  for (;;) {
    const page: Page<T> = await fetchPage(before);
    items.push(...page.items);
    const more = page.next_before_id !== null;
    if (items.length > cap || (items.length === cap && more)) {
      items.length = cap;
      onProgress?.(items.length);
      return { items, truncated: true };
    }
    onProgress?.(items.length);
    // A cursor that does not move would loop for ever.
    if (!more || page.next_before_id === before) {
      return { items, truncated: false };
    }
    before = page.next_before_id;
  }
}

/** Hand the browser a file through a Blob and a clicked link. */
export function downloadFile(name: string, mime: string, body: string): void {
  const url = URL.createObjectURL(new Blob([body], { type: mime }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.hidden = true;
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    URL.revokeObjectURL(url);
  }
}

export type ExportFormat = "csv" | "json";

export const exportName = (
  tab: "audit" | "activity",
  format: ExportFormat,
  now: Date = new Date(),
): string =>
  `electric-fence-${tab}-${now.toISOString().slice(0, 10)}-unmasked.${format}`;

const names = (value: unknown): string =>
  Array.isArray(value) ? value.map(String).join("; ") : "";

/** The details as JSON, without `values` (they have their own column). */
const detailsJson = (details: Record<string, unknown>): string => {
  const rest = Object.fromEntries(
    Object.entries(details).filter(([key]) => key !== "values"),
  );
  return JSON.stringify(rest);
};

export function auditCsv(items: ChangeItem[]): string {
  const withValues = items.some((i) => i.details["values"] !== undefined);
  const header = [
    "time",
    "kind",
    "action",
    "target",
    "target_name",
    "actor",
    "actor_ip",
    "changed",
    "details",
    ...(withValues ? ["values"] : []),
  ];
  return toCsv(
    header,
    items.map((i) => [
      i.ts,
      i.kind,
      i.action,
      i.target_id,
      i.target_name,
      i.actor_name ?? i.actor_user_id,
      i.actor_ip,
      names(i.details["changed"]),
      detailsJson(i.details),
      ...(withValues
        ? [
            i.details["values"] === undefined
              ? ""
              : JSON.stringify(i.details["values"]),
          ]
        : []),
    ]),
  );
}

export const activityCsv = (items: LoginItem[]): string =>
  toCsv(
    [
      "time",
      "result",
      "reason",
      "username",
      "user_exists",
      "user",
      "ip",
      "country",
      "hostname",
      "source",
      "client",
      "user_agent",
      "mfa_module",
      "new_country",
      "count",
    ],
    items.map((i) => [
      i.ts,
      i.result,
      i.reason,
      i.username_typed,
      i.user_exists,
      i.user_name ?? i.user_id,
      i.ip,
      i.country_code,
      i.hostname,
      i.source,
      i.client_name ?? i.client_id,
      i.user_agent,
      i.mfa_module,
      i.new_country,
      i.count,
    ]),
  );

export const exportJson = (
  filters: object,
  items: unknown[],
  truncated: boolean,
  now: Date = new Date(),
): string =>
  JSON.stringify(
    { exported_at: now.toISOString(), filters, truncated, items },
    null,
    2,
  );
