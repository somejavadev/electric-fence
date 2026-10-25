import { html, nothing } from "lit";
import { fetchChanges, fetchUsers } from "../api.ts";
import "../components/ef-change-row.ts";
import "../components/ef-filter-bar.ts";
import type { FilterChange, FilterField } from "../components/ef-filter-bar.ts";
import { TIME_RANGES, sinceFor, truncate } from "../format.ts";
import type { TimeRange } from "../format.ts";
import { strings } from "../strings.ts";
import { viewStyles } from "../styles.ts";
import type {
  ChangeFilter,
  ChangeItem,
  ChangeKind,
  KnownChangeKind,
  LoginItem,
  UserItem,
} from "../types.ts";
import { LIVE_CAP, LiveView } from "./live-view.ts";

const KINDS = [
  "config_entry",
  "user",
  "core_config",
  "backup",
  "http",
  "network",
  "analytics",
  "logger",
  "recorder",
] as const satisfies readonly KnownChangeKind[];

export interface AuditFilters {
  kind: ChangeKind | "";
  actor: string;
  range: TimeRange;
  /** Include changes made by the system (no user). Off by default. */
  system: boolean;
}

export const DEFAULT_AUDIT_FILTERS: AuditFilters = {
  kind: "",
  actor: "",
  range: "7d",
  system: false,
};

export function changeFilter(
  filters: AuditFilters,
  now: number = Date.now(),
): ChangeFilter {
  const out: ChangeFilter = { include_system: filters.system };
  if (filters.kind !== "") {
    out.kind = filters.kind;
  }
  if (filters.actor !== "") {
    out.actor_user_id = filters.actor;
  }
  const since = sinceFor(filters.range, now);
  if (since !== undefined) {
    out.since = since;
  }
  return out;
}

export function matchesChange(
  item: ChangeItem,
  filters: AuditFilters,
  now: number = Date.now(),
): boolean {
  if (item.actor_user_id === null && !filters.system) {
    return false;
  }
  if (filters.kind !== "" && item.kind !== filters.kind) {
    return false;
  }
  if (filters.actor !== "" && item.actor_user_id !== filters.actor) {
    return false;
  }
  const since = sinceFor(filters.range, now);
  return since === undefined || Date.parse(item.ts) >= Date.parse(since);
}

export interface DetailGroup {
  label: string;
  values: string[];
}

const names = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string" && v !== "")
    : typeof value === "string" && value !== ""
      ? [value]
      : [];

/**
 * What changed, from the collectors' payloads: `changed`, `data_keys_changed`,
 * `options_keys_changed`, `field` and `keys`. Other detail keys are not shown.
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

export interface ValueLine {
  name: string;
  before?: string;
  after?: string;
  hidden: boolean;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** "location_name" becomes "Location name"; "options.x_y" "Option: X y". */
function humanise(name: string): string {
  const prefix = "options.";
  const bare = name.startsWith(prefix) ? name.slice(prefix.length) : name;
  const text = bare.replaceAll("_", " ");
  const words = text.charAt(0).toUpperCase() + text.slice(1);
  return name.startsWith(prefix)
    ? `${strings.audit.optionPrefix}: ${words}`
    : words;
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

const NEW_KINDS: readonly string[] = KINDS.slice(3);

/** Rows written by the system settings audit, not by v0.1 collectors. */
function isModern(item: ChangeItem): boolean {
  const d = item.details;
  return (
    NEW_KINDS.includes(item.kind) ||
    isRecord(d["values"]) ||
    names(d["changed"]).some((n) => n.startsWith("options.")) ||
    (item.kind === "core_config" && Array.isArray(d["changed"]))
  );
}

/** Audit tab: who changed which settings, with live additions. */
export class EfAudit extends LiveView {
  static override properties = {
    ...LiveView.properties,
    _filters: { state: true },
    _items: { state: true },
    _live: { state: true },
    _next: { state: true },
    _users: { state: true },
    _loadingMore: { state: true },
  };

  declare _filters: AuditFilters;
  declare _items: ChangeItem[];
  /** Pushed items, newest first. They have no id and are never cursors. */
  declare _live: ChangeItem[];
  declare _next: number | null;
  declare _users: UserItem[];
  declare _loadingMore: boolean;

  static override styles = viewStyles;

  constructor() {
    super();
    this._filters = { ...DEFAULT_AUDIT_FILTERS };
    this._items = [];
    this._live = [];
    this._next = null;
    this._users = [];
    this._loadingMore = false;
  }

  protected override async _fetch(): Promise<() => void> {
    const page = await fetchChanges(this.hass, changeFilter(this._filters));
    return () => {
      this._items = page.items;
      this._next = page.next_before_id;
      this._live = [];
    };
  }

  protected override _loadExtras(generation: number): void {
    void (async () => {
      try {
        const { items } = await fetchUsers(this.hass);
        if (generation === this._generation) {
          this._users = items;
        }
      } catch {
        // Without the list the actor dropdown only offers "Anyone".
      }
    })();
  }

  protected override _onLive(item: LoginItem | ChangeItem, kind: string): void {
    if (kind !== "change" || this._phase !== "ready") {
      return;
    }
    const change = item as ChangeItem;
    if (matchesChange(change, this._filters)) {
      this._live = [change, ...this._live].slice(0, LIVE_CAP);
    }
  }

  private _onFilter = (event: CustomEvent<FilterChange>): void => {
    const { name, value } = event.detail;
    const next = { ...this._filters };
    if (name === "kind" && (value === "" || KINDS.some((k) => k === value))) {
      next.kind = value as AuditFilters["kind"];
    } else if (name === "actor") {
      next.actor = value;
    } else if (name === "range" && TIME_RANGES.some((r) => r === value)) {
      next.range = value as TimeRange;
    } else if (name === "system") {
      next.system = value === "1";
    } else {
      return;
    }
    this._filters = next;
    this._resetList();
    void this._refresh();
  };

  protected override _resetRun(): void {
    this._loadingMore = false;
  }

  /** Drop what the old filters showed, so no cursor or row outlives them. */
  private _resetList(): void {
    this._items = [];
    this._live = [];
    this._next = null;
    this._loadingMore = false;
    this._phase = "loading";
  }

  private async _loadMore(): Promise<void> {
    const before = this._next;
    if (before === null || this._loadingMore) {
      return;
    }
    const generation = this._generation;
    const request = this._requests;
    this._loadingMore = true;
    try {
      const page = await fetchChanges(this.hass, {
        ...changeFilter(this._filters),
        before_id: before,
      });
      if (generation === this._generation && request === this._requests) {
        this._items = [...this._items, ...page.items];
        this._next = page.next_before_id;
      }
    } catch (err) {
      if (generation === this._generation && request === this._requests) {
        this._fail(err);
      }
    } finally {
      // A stale request must not clear the flag of a newer one.
      if (generation === this._generation && request === this._requests) {
        this._loadingMore = false;
      }
    }
  }

  private _fields(): FilterField[] {
    const f = strings.filters;
    return [
      {
        name: "kind",
        label: f.kind,
        type: "select",
        options: [
          { value: "", label: f.anyKind },
          ...KINDS.map((k) => ({ value: k, label: f.kinds[k] })),
        ],
      },
      {
        name: "actor",
        label: f.actor,
        type: "select",
        options: [
          { value: "", label: f.anyActor },
          ...this._users.map((u) => ({
            value: u.id,
            label: truncate(u.name ?? u.username ?? u.id, 64).short,
          })),
        ],
      },
      {
        name: "range",
        label: f.range,
        type: "select",
        options: TIME_RANGES.map((r) => ({ value: r, label: f.ranges[r] })),
      },
      { name: "system", label: f.showSystem, type: "checkbox" },
    ];
  }

  private _line(line: ValueLine) {
    const name = truncate(line.name, 64);
    const shown = line.hidden
      ? { short: strings.audit.hiddenValue, full: strings.audit.hiddenValue }
      : undefined;
    if (shown !== undefined) {
      const full = `${name.full}: ${shown.full}`;
      return html`<div class="line">
        <bdi title=${full}>${name.short}: ${shown.short}</bdi>
      </div>`;
    }
    if (line.before === undefined || line.after === undefined) {
      return html`<div class="line">
        <bdi title=${name.full}>${name.short}</bdi>
      </div>`;
    }
    const before = truncate(line.before, 64);
    const after = truncate(line.after, 64);
    const full = `${name.full}: ${before.full} → ${after.full}`;
    return html`<div class="line">
      <bdi title=${full}>${name.short}: ${before.short} → ${after.short}</bdi>
    </div>`;
  }

  private _modernDetails(item: ChangeItem) {
    const a = strings.audit;
    const d = item.details;
    const lines = valueLines(d);
    const hasValues = isRecord(d["values"]) && lines.length > 0;
    const dataKeys = names(d["data_keys_changed"]);
    if (lines.length === 0 && dataKeys.length === 0) {
      return nothing;
    }
    const group = (label: string, list: string[]) => {
      const text = truncate(list.join(", "), 128);
      return html`<div class="line">
        ${label}: <bdi title=${text.full}>${text.short}</bdi>
      </div>`;
    };
    return html`<div class="details">
      ${
        hasValues
          ? lines.map((line) => this._line(line))
          : lines.length > 0
            ? group(
                a.changedLabel,
                lines.map((line) => line.name),
              )
            : nothing
      }
      ${dataKeys.length > 0 ? group(a.dataKeysLabel, dataKeys) : nothing}
    </div>`;
  }

  private _details(item: ChangeItem) {
    if (isModern(item)) {
      return this._modernDetails(item);
    }
    const groups = detailGroups(item.details);
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

  protected override render() {
    const items = [...this._live, ...this._items];
    const values = {
      kind: this._filters.kind,
      actor: this._filters.actor,
      range: this._filters.range,
      system: this._filters.system ? "1" : "",
    };
    return html`
      <h2 class="visually-hidden">${strings.audit.heading}</h2>
      ${this._banners()}
      <ef-filter-bar
        .fields=${this._fields()}
        .values=${values}
        @filter-changed=${this._onFilter}
      ></ef-filter-bar>
      ${
        this._phase === "loading"
          ? html`<p class="loading">${strings.loading}</p>`
          : items.length === 0 && this._phase === "ready"
            ? html`<div class="card">
                <p class="empty">${strings.audit.empty}</p>
              </div>`
            : html`<div class="card">
                <ul>
                  ${items.map(
                    (item) =>
                      html`<li>
                        <ef-change-row
                          .hass=${this.hass}
                          .item=${item}
                        ></ef-change-row>
                        ${this._details(item)}
                      </li>`,
                  )}
                </ul>
              </div>`
      }
      ${
        this._next !== null
          ? html`<div class="more">
              <button
                type="button"
                ?disabled=${this._loadingMore}
                @click=${() => void this._loadMore()}
              >
                ${strings.loadMore}
              </button>
            </div>`
          : nothing
      }
    `;
  }
}

customElements.define("ef-audit", EfAudit);

declare global {
  interface HTMLElementTagNameMap {
    "ef-audit": EfAudit;
  }
}
