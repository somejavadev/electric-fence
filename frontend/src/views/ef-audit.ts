import { html, nothing } from "lit";
import { auditCsv } from "../export.ts";
import type { ExportFormat } from "../export.ts";
import { fetchChanges, fetchUsers } from "../api.ts";
import "../components/ef-change-row.ts";
import { NarrowController } from "../components/ef-filter-pane.ts";
import "../components/ef-filter-pane.ts";
import type {
  FilterChange,
  FilterField,
} from "../components/ef-filter-pane.ts";
import { changeLines } from "../change-lines.ts";
import {
  TIME_RANGES,
  containsText,
  isDayOrEmpty,
  rangeBounds,
  truncate,
  withinBounds,
} from "../format.ts";
import type { RangeChoice } from "../format.ts";
import { actorLabel, isSystemChange, strings } from "../strings.ts";
import { viewStyles } from "../styles.ts";
import type {
  ChangeAction,
  ChangeFilter,
  ChangeItem,
  ChangeKind,
  KnownChangeKind,
  LoginItem,
  UserItem,
} from "../types.ts";
import { LIVE_CAP, LiveView } from "./live-view.ts";

export { detailGroups, valueLines } from "../change-lines.ts";

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
  "ip_ban",
  "login_protection",
  "app",
  "files",
  "integration_health",
  "forwarding",
] as const satisfies readonly KnownChangeKind[];

const ACTIONS = [
  "added",
  "removed",
  "updated",
  "reconfigured",
  "options_changed",
  "enabled",
  "disabled",
  "password_changed",
  "restore_requested",
  "confirmed",
  "purged",
  "withdrawn",
  "banned",
  "unbanned",
  "expired",
  "attack_started",
  "attack_ended",
  "slowdown_ended",
  "countries_cleared",
  "installed",
  "uninstalled",
  "started",
  "stopped",
  "restarted",
  "rebuilt",
  "settings_changed",
  "repository_added",
  "repository_removed",
  "files_changed",
  "files_match",
  "baseline_recorded",
  "baseline_renewed",
  "baseline_accepted",
  "archived",
  "removed_from_store",
  "unmaintained",
  "recovered",
  "forwarding_failed",
  "forwarding_recovered",
] as const satisfies readonly ChangeAction[];

const CUSTOM = { field: "range", equals: "custom" } as const;

export interface AuditFilters {
  kind: ChangeKind | "";
  action: ChangeAction | "";
  actor: string;
  ip: string;
  range: RangeChoice;
  /** `YYYY-MM-DD` days, used only when the range is custom. */
  from: string;
  to: string;
  /** Include changes made by the system (no user). Off by default. */
  system: boolean;
  search: string;
}

export const DEFAULT_AUDIT_FILTERS: AuditFilters = {
  kind: "",
  action: "",
  actor: "",
  ip: "",
  range: "7d",
  from: "",
  to: "",
  system: false,
  search: "",
};

const DEFAULT_VALUES: Record<string, string> = {
  ...DEFAULT_AUDIT_FILTERS,
  system: "",
};

export function changeFilter(
  filters: AuditFilters,
  now: number = Date.now(),
): ChangeFilter {
  const out: ChangeFilter = { include_system: filters.system };
  if (filters.kind !== "") {
    out.kind = filters.kind;
  }
  if (filters.action !== "") {
    out.action = filters.action;
  }
  if (filters.actor !== "") {
    out.actor_user_id = filters.actor;
  }
  if (filters.ip !== "") {
    out.ip = filters.ip;
  }
  if (filters.search !== "") {
    out.search = filters.search;
  }
  return {
    ...out,
    ...rangeBounds(filters.range, filters.from, filters.to, now),
  };
}

export function matchesChange(
  item: ChangeItem,
  filters: AuditFilters,
  now: number = Date.now(),
): boolean {
  if (isSystemChange(item) && !filters.system) {
    return false;
  }
  if (filters.kind !== "" && item.kind !== filters.kind) {
    return false;
  }
  if (filters.action !== "" && item.action !== filters.action) {
    return false;
  }
  if (filters.actor !== "" && item.actor_user_id !== filters.actor) {
    return false;
  }
  if (filters.ip !== "" && item.actor_ip !== filters.ip) {
    return false;
  }
  if (
    !containsText(
      [
        item.target_id,
        item.target_name,
        item.actor_name,
        item.actor_user_id === null && item.actor_name !== null
          ? actorLabel(item)
          : null,
        item.actor_ip,
        JSON.stringify(item.details),
      ],
      filters.search,
    )
  ) {
    return false;
  }
  return withinBounds(
    item.ts,
    rangeBounds(filters.range, filters.from, filters.to, now),
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
    if (
      name === "kind" &&
      (value === "" || (KINDS as readonly string[]).includes(value))
    ) {
      next.kind = value as AuditFilters["kind"];
    } else if (
      name === "action" &&
      (value === "" || (ACTIONS as readonly string[]).includes(value))
    ) {
      next.action = value as AuditFilters["action"];
    } else if (name === "actor") {
      next.actor = value;
    } else if (name === "ip") {
      next.ip = value;
    } else if (name === "search") {
      next.search = value;
    } else if (
      name === "range" &&
      (value === "custom" || (TIME_RANGES as readonly string[]).includes(value))
    ) {
      next.range = value as RangeChoice;
    } else if ((name === "from" || name === "to") && isDayOrEmpty(value)) {
      next[name] = value;
    } else if (name === "system") {
      next.system = value === "1";
    } else {
      return;
    }
    this._apply(next);
  };

  private _onCleared = (): void => {
    this._apply({ ...DEFAULT_AUDIT_FILTERS });
  };

  private _apply(next: AuditFilters): void {
    this._filters = next;
    this._resetList();
    void this._refresh();
  }

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

  private _startDownload = (format: ExportFormat): void => {
    void this._download<ChangeItem>(
      format,
      "audit",
      changeFilter(this._filters),
      (filter) => fetchChanges(this.hass, filter),
      auditCsv,
    );
  };

  private _narrow = new NarrowController(this);

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
        name: "action",
        label: f.action,
        type: "select",
        options: [
          { value: "", label: f.anyAction },
          ...ACTIONS.map((a) => ({ value: a, label: f.actions[a] })),
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
      { name: "ip", label: f.ip, type: "text" },
      {
        name: "range",
        label: f.range,
        type: "select",
        options: [
          ...TIME_RANGES.map((r) => ({ value: r, label: f.ranges[r] })),
          { value: "custom", label: f.customRange },
        ],
      },
      { name: "from", label: f.from, type: "date", when: CUSTOM },
      { name: "to", label: f.to, type: "date", when: CUSTOM },
      { name: "system", label: f.showSystem, type: "checkbox" },
    ];
  }

  private _details(item: ChangeItem) {
    return changeLines(item, this.hass);
  }

  protected override render() {
    const items = [...this._live, ...this._items];
    const values = {
      ...this._filters,
      system: this._filters.system ? "1" : "",
    };
    return html`
      <h2 class="visually-hidden">${strings.audit.heading}</h2>
      ${this._banners()}
      <ef-filter-pane
        .fields=${this._fields()}
        .values=${values}
        .defaults=${{ ...DEFAULT_VALUES }}
        .narrow=${this._narrow.narrow}
        @filter-changed=${this._onFilter}
        @filters-cleared=${this._onCleared}
      >
        ${this._downloadMenu(this._startDownload)}
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
      </ef-filter-pane>
    `;
  }
}

customElements.define("ef-audit", EfAudit);

declare global {
  interface HTMLElementTagNameMap {
    "ef-audit": EfAudit;
  }
}
