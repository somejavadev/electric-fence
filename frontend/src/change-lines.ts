import { html, nothing } from "lit";
import type { TemplateResult } from "lit";
import { truncate } from "./format.ts";
import { strings } from "./strings.ts";

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
    return {
      name,
      before: formatValue(entry["before"]),
      after: formatValue(entry["after"]),
      hidden: false,
    };
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
