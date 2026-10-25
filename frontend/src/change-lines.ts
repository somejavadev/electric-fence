import { html, nothing } from "lit";
import type { TemplateResult } from "lit";
import { formatTime, truncate } from "./format.ts";
import { strings } from "./strings.ts";
import type { ChangeItem, HomeAssistant } from "./types.ts";

/** Kinds Electric Fence records itself; their details are not settings. */
const PROTECTION_KINDS: readonly string[] = ["ip_ban", "login_protection"];

export const isProtectionChange = (item: ChangeItem): boolean =>
  PROTECTION_KINDS.includes(item.kind);

export interface DetailGroup {
  label: string;
  values: string[];
}

export interface ValueLine {
  name: string;
  before?: string;
  after?: string;
  hidden: boolean;
}

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export const names = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string" && v !== "")
    : typeof value === "string" && value !== ""
      ? [value]
      : [];

/**
 * Key lists of rows written before `changed` carried the setting names:
 * `data_keys_changed`, `options_keys_changed`, `field` and `keys`, plus
 * `changed` itself. Other detail keys are not shown.
 */
export function detailGroups(details: Record<string, unknown>): DetailGroup[] {
  const a = strings.audit;
  return [
    { label: a.changed, values: names(details["changed"]) },
    { label: a.dataKeys, values: names(details["data_keys_changed"]) },
    { label: a.optionKeys, values: names(details["options_keys_changed"]) },
    { label: a.field, values: names(details["field"]) },
    { label: a.keys, values: names(details["keys"]) },
  ].filter((group) => group.values.length > 0);
}

/** "location_name" is "Home name"; "options.x_y" is "Option: X y". */
function humanise(name: string): string {
  const prefix = "options.";
  if (name.startsWith(prefix)) {
    const bare = name.slice(prefix.length).replaceAll("_", " ");
    return `${strings.audit.optionPrefix}: ${bare.charAt(0).toUpperCase()}${bare.slice(1)}`;
  }
  const labels: Record<string, string> = strings.audit.settingNames;
  if (Object.hasOwn(labels, name)) {
    return labels[name] ?? name;
  }
  const text = name.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A value in a short form: lists and objects as compact text. */
function formatValue(value: unknown): string {
  if (value === undefined || value === null) {
    return strings.audit.noValue;
  }
  if (typeof value === "string") {
    return value === "" ? strings.audit.noValue : value;
  }
  if (Array.isArray(value)) {
    return value.length === 0
      ? strings.audit.noValue
      : value.map(formatValue).join(", ");
  }
  if (isRecord(value)) {
    if (value["hidden"] === true && Object.keys(value).length === 1) {
      return strings.audit.hiddenValue;
    }
    const label = value["name"];
    if (typeof label === "string" && label !== "") {
      return label;
    }
    const parts = Object.entries(value).map(
      ([key, v]) => `${key}: ${formatValue(v)}`,
    );
    return parts.length === 0 ? strings.audit.noValue : parts.join(", ");
  }
  return String(value);
}

/** Deep equality of JSON values (object key order does not matter). */
function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) {
    return true;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => sameValue(v, b[i]));
  }
  if (isRecord(a) && isRecord(b)) {
    const keys = Object.keys(a);
    return (
      keys.length === Object.keys(b).length &&
      keys.every((k) => Object.hasOwn(b, k) && sameValue(a[k], b[k]))
    );
  }
  return false;
}

/**
 * Before and after of a setting. Two objects with the same name (a backup
 * schedule's `create_backup`, say) show only the nested fields that differ,
 * so a change is not hidden behind "MyName → MyName". The name stands for
 * the object only when it is the only field or it changed.
 */
function beforeAfter(before: unknown, after: unknown): [string, string] {
  if (isRecord(before) && isRecord(after)) {
    const keys = Object.keys(before);
    for (const key of Object.keys(after)) {
      if (!keys.includes(key)) {
        keys.push(key);
      }
    }
    const differ = keys.filter((k) => !sameValue(before[k], after[k]));
    const onlyName = keys.length === 1 && keys[0] === "name";
    const sameName = sameValue(before["name"], after["name"]);
    if (!onlyName && sameName && differ.length > 0) {
      const side = (value: Record<string, unknown>): string =>
        differ.map((k) => `${k}: ${formatValue(value[k])}`).join(", ");
      return [side(before), side(after)];
    }
  }
  return [formatValue(before), formatValue(after)];
}

/**
 * One line per changed setting. A name without a `values` entry (changed by
 * hash only) has no before or after and is shown as a plain name.
 */
export function valueLines(details: Record<string, unknown>): ValueLine[] {
  const values = isRecord(details["values"]) ? details["values"] : {};
  const order = [...names(details["changed"])];
  for (const key of Object.keys(values)) {
    if (!order.includes(key)) {
      order.push(key);
    }
  }
  return order.map((key): ValueLine => {
    const name = humanise(key);
    const entry = Object.hasOwn(values, key) ? values[key] : undefined;
    if (!isRecord(entry)) {
      return { name, hidden: false };
    }
    if (entry["hidden"] === true) {
      return { name, hidden: true };
    }
    const [before, after] = beforeAfter(entry["before"], entry["after"]);
    return { name, before, after, hidden: false };
  });
}

const MAX_VALUE = 64;

/** Each part is its own bdi, so no part can reorder the others. */
function lineTemplate(line: ValueLine): TemplateResult {
  const name = truncate(line.name, MAX_VALUE);
  const nameEl = html`<bdi title=${name.full}>${name.short}</bdi>`;
  if (line.hidden) {
    return html`<div class="line">
      ${nameEl}: ${strings.audit.hiddenValue}
    </div>`;
  }
  if (line.before === undefined || line.after === undefined) {
    return html`<div class="line">${nameEl}</div>`;
  }
  const before = truncate(line.before, MAX_VALUE);
  const after = truncate(line.after, MAX_VALUE);
  return html`<div class="line">
    ${nameEl}: <bdi title=${before.full}>${before.short}</bdi> →
    <bdi title=${after.full}>${after.short}</bdi>
  </div>`;
}

function groupTemplate(label: string, list: string[]): TemplateResult {
  const text = truncate(list.join(", "), 128);
  return html`<div class="line">
    ${label}: <bdi title=${text.full}>${text.short}</bdi>
  </div>`;
}

/**
 * What changed in a row. Rows with `changed` show value lines (when values
 * were recorded) or "Changed: names"; older rows show their key lists.
 * `limit` caps the setting lines and adds "and N more".
 */
export function changeDetails(
  details: Record<string, unknown>,
  limit?: number,
): TemplateResult | typeof nothing {
  const a = strings.audit;
  const lines = valueLines(details);
  if (names(details["changed"]).length === 0 && !isRecord(details["values"])) {
    const groups = detailGroups(details);
    if (groups.length === 0) {
      return nothing;
    }
    return html`<div class="details">
      ${groups.map((group, i) => {
        const list = truncate(group.values.join(", "), 128);
        return html`${i > 0 ? " · " : nothing}${group.label}:
          <bdi title=${list.full}>${list.short}</bdi>`;
      })}
    </div>`;
  }
  const hasValues = isRecord(details["values"]) && lines.length > 0;
  const extra = [
    { label: a.dataKeysLabel, list: names(details["data_keys_changed"]) },
    { label: a.optionKeysLabel, list: names(details["options_keys_changed"]) },
  ].filter((g) => g.list.length > 0);
  const shown = limit === undefined ? lines : lines.slice(0, limit);
  const more = lines.length - shown.length;
  if (lines.length === 0 && extra.length === 0) {
    return nothing;
  }
  return html`<div class="details">
    ${
      hasValues
        ? shown.map(lineTemplate)
        : shown.length > 0
          ? groupTemplate(
              a.changedLabel,
              shown.map((line) => line.name),
            )
          : nothing
    }
    ${more > 0 ? html`<div class="line">${a.andMore(more)}</div>` : nothing}
    ${extra.map((g) => groupTemplate(g.label, g.list))}
  </div>`;
}

const count = (value: unknown): string | undefined =>
  typeof value === "number" && Number.isFinite(value)
    ? String(value)
    : undefined;

/** The facts a ban or login protection change carries, on one line. */
function protectionDetails(
  details: Record<string, unknown>,
  hass: HomeAssistant,
): TemplateResult | typeof nothing {
  const p = strings.audit.protection;
  const parts: [string, string][] = [];
  const add = (label: string, value: string | undefined) => {
    if (value !== undefined && value !== "") {
      parts.push([label, value]);
    }
  };
  add(p.failures, count(details["failures"]));
  if (Object.hasOwn(details, "expires_at")) {
    const expires = details["expires_at"];
    add(
      p.expires,
      typeof expires === "string" ? formatTime(expires, hass) : p.never,
    );
  }
  add(p.banNumber, count(details["ban_number"]));
  add(p.addresses, count(details["ip_count"]));
  add(p.countries, names(details["countries"]).join(", "));
  const reason = details["reason"];
  if (typeof reason === "string") {
    add(p.how, Object.hasOwn(p.reasons, reason) ? p.reasons[reason] : reason);
  }
  const source = details["source"];
  if (source === "electric_fence" || source === "other") {
    add(p.source, strings.bans.sources[source]);
  }
  add(p.cleared, count(details["cleared"]));
  if (parts.length === 0) {
    return nothing;
  }
  return html`<div class="details">
    ${parts.map(([label, value], i) => {
      const text = truncate(value, 128);
      return html`${i > 0 ? " · " : nothing}${label}:
        <bdi title=${text.full}>${text.short}</bdi>`;
    })}
  </div>`;
}

/** Details of any change: protection facts, or the changed settings. */
export function changeLines(
  item: ChangeItem,
  hass: HomeAssistant,
  limit?: number,
): TemplateResult | typeof nothing {
  return isProtectionChange(item)
    ? protectionDetails(item.details, hass)
    : changeDetails(item.details, limit);
}
